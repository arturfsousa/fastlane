# Standard Library
import random
import re
import traceback
from json import loads

# 3rd Party
import docker
import pybreaker
import requests
from dateutil.parser import parse
from flask import Blueprint, current_app, g, make_response, request

# Fastlane
from fastlane.worker import ExecutionResult
from fastlane.worker.errors import HostUnavailableError

# http://docs.docker.com/engine/reference/commandline/ps/#examples
# One of created, restarting, running, removing, paused, exited, or dead
STATUS = {
    "created": ExecutionResult.Status.created,
    "exited": ExecutionResult.Status.done,
    "dead": ExecutionResult.Status.failed,
    "running": ExecutionResult.Status.running,
}

bp = Blueprint("docker", __name__, url_prefix="/docker-executor")
blacklist_key = "docker-executor::blacklisted-hosts"
job_prefix = "fastlane-job"


def get_details():
    details = request.get_json()

    if details is None and request.get_data():
        details = loads(request.get_data())

    return details


@bp.route("/blacklist", methods=["POST", "PUT"])
def add_to_blacklist():
    redis = current_app.redis

    data = get_details()

    if data is None or data == "":
        msg = "Failed to add host to blacklist because JSON body could not be parsed."
        g.logger.warn(msg)

        return make_response(msg, 400)

    if "host" not in data:
        msg = "Failed to add host to blacklist because 'host' attribute was not found in JSON body."
        g.logger.warn(msg)

        return make_response(msg, 400)

    host = data["host"]

    redis.sadd(blacklist_key, host)

    return ""


@bp.route("/blacklist", methods=["DEL", "DELETE"])
def remove_from_blacklist():
    redis = current_app.redis

    data = get_details()

    if data is None or data == "":
        msg = "Failed to remove host from blacklist because JSON body could not be parsed."
        g.logger.warn(msg)

        return make_response(msg, 400)

    if "host" not in data:
        msg = "Failed to remove host from blacklist because 'host' attribute was not found in JSON body."
        g.logger.warn(msg)

        return make_response(msg, 400)

    host = data["host"]

    redis.srem(blacklist_key, host)

    return ""


class DockerPool:
    def __init__(self, docker_hosts):
        self.docker_hosts = docker_hosts

        self.max_running = {}
        self.clients_per_regex = []
        self.clients = {}
        self.__init_clients()

    def __init_clients(self):
        for regex, docker_hosts, max_running in self.docker_hosts:
            client_list = []
            clients = (regex, client_list)
            self.clients_per_regex.append(clients)
            self.max_running[regex] = max_running

            for address in docker_hosts:
                host, port = address.split(":")
                cl = docker.DockerClient(base_url=address)
                self.clients[address] = (host, int(port), cl)
                client_list.append((host, int(port), cl))

    def refresh_circuits(self, executor, clients, blacklisted_hosts, logger):
        def ps(client):
            client.containers.list(sparse=False)

        for host, port, client in clients:
            if f"{host}:{port}" in blacklisted_hosts:
                continue

            try:
                logger.debug("Refreshing host...", host=host, port=port)
                circuit = executor.get_circuit(f"{host}:{port}")
                circuit.call(ps, client)
            except pybreaker.CircuitBreakerError:
                error = traceback.format_exc()
                logger.error("Failed to refresh host.", error=error)

    def get_client(self, executor, task_id, host=None, port=None, blacklist=None):
        logger = current_app.logger.bind(
            task_id=task_id, host=host, port=port, blacklist=blacklist
        )

        if host is not None and port is not None:
            logger.debug("Custom host returned.")

            return self.clients.get(f"{host}:{port}")

        if blacklist is None:
            blacklist = set()

        for regex, clients in self.clients_per_regex:
            logger.debug("Trying to retrieve docker client...", regex=regex)

            if regex is not None and not regex.match(task_id):
                logger.debug("Task ID does not match regex.", regex=regex)

                continue

            self.refresh_circuits(executor, clients, blacklist, logger)
            filtered = [
                (host, port, client)

                for (host, port, client) in clients

                if f"{host}:{port}" not in blacklist
                and executor.get_circuit(f"{host}:{port}").current_state == "closed"
            ]

            if not filtered:
                logger.debug(
                    "No non-blacklisted and closed circuit clients found for farm.",
                    regex=regex,
                )

                continue

            logger.info(
                "Returning random choice out of the remaining clients.",
                clients=[f"{host}:{port}" for (host, port, client) in filtered],
            )

            host, port, client = random.choice(filtered)

            return host, int(port), client

        msg = f"Failed to find a docker host for task id {task_id}."
        logger.error(msg)
        raise RuntimeError(msg)


class Executor:
    def __init__(self, app, pool=None):
        self.app = app
        self.logger = app.logger
        self.pool = pool
        self.circuits = {}

        if pool is None:
            docker_hosts = []
            clusters = loads(self.app.config["DOCKER_HOSTS"])

            self.logger.debug("Initializing docker pool...", clusters=clusters)

            for i, cluster in enumerate(clusters):
                regex = cluster["match"]

                if not regex:
                    regex = None

                    if i != len(clusters):
                        self.logger.warn(
                            "Farm with no regex found before the end of the DOCKER_HOSTS "
                            "definition. This means all the subsequent farms will never be"
                            " used as this one will always match anything that reaches it. "
                            "Please ensure that the farm with no regex match is the last one."
                        )
                else:
                    regex = re.compile(regex)

                hosts = cluster["hosts"]
                max_running = cluster.get("maxRunning", 10)
                self.logger.info(
                    "Found farm definition.",
                    regex=cluster["match"],
                    hosts=hosts,
                    max_running=max_running,
                )
                docker_hosts.append((regex, hosts, max_running))

            self.pool = DockerPool(docker_hosts)

    def validate_max_running_executions(self, task_id):
        total_running = 0
        max_running = 0
        logger = self.logger.bind(
            task_id=task_id, operation="docker_host.validate_max_running_executions"
        )

        for regex, _ in self.pool.clients_per_regex:
            if regex is not None and not regex.match(task_id):
                logger.debug("Farm does not match task_id.", regex=regex)

                continue

            running = self.get_running_containers(regex)
            total_running = len(running["running"])
            max_running = self.pool.max_running[regex]
            logger.debug(
                "Found number of running containers.",
                total_running=total_running,
                max_running=max_running,
            )

            break

        return total_running == 0 or total_running <= max_running

    def get_circuit(self, key):
        max_fails = int(current_app.config["DOCKER_CIRCUIT_BREAKER_MAX_FAILS"])
        reset_timeout = int(
            current_app.config["DOCKER_CIRCUIT_BREAKER_RESET_TIMEOUT_SECONDS"]
        )

        return self.circuits.setdefault(
            key,
            pybreaker.CircuitBreaker(
                fail_max=max_fails,
                reset_timeout=reset_timeout,
                state_storage=pybreaker.CircuitRedisStorage(
                    pybreaker.STATE_CLOSED, current_app.redis, namespace=key
                ),
            ),
        )

    def update_image(self, task, job, execution, image, tag, blacklisted_hosts=None):
        if blacklisted_hosts is None:
            blacklisted_hosts = self.get_blacklisted_hosts()

        logger = self.logger.bind(
            task_id=task.task_id,
            job_id=str(job.job_id),
            execution_id=str(execution.execution_id),
            image=image,
            tag=tag,
            blacklisted_hosts=blacklisted_hosts,
            operation="docker_executor.update_image",
        )

        host, port, cl = self.pool.get_client(
            self, task.task_id, blacklist=blacklisted_hosts
        )
        circuit = self.get_circuit(f"{host}:{port}")

        logger = logger.bind(host=host, port=port)

        @circuit
        def run(logger):
            try:
                logger.debug("Updating image in docker host...")
                cl.images.pull(image, tag=tag)
                execution.metadata["docker_host"] = host
                execution.metadata["docker_port"] = port
                logger.info(
                    "Image updated successfully. Docker host and port stored in Job Execution for future reference."
                )
            except requests.exceptions.ConnectionError as err:
                error = traceback.format_exc()
                logger.error(
                    "Failed to connect to Docker Host. Will retry job later with a new host.",
                    error=error,
                )

                if "docker_host" in execution.metadata:
                    del execution.metadata["docker_host"]

                if "docker_port" in execution.metadata:
                    del execution.metadata["docker_port"]

                raise HostUnavailableError(host, port, err)

        run(logger)

    def run(self, task, job, execution, image, tag, command, blacklisted_hosts=None):
        host, port, cl = None, None, None

        logger = self.logger.bind(
            task_id=task.task_id,
            job_id=str(job.job_id),
            execution_id=str(execution.execution_id),
            image=image,
            tag=tag,
            command=command,
            blacklisted_hosts=blacklisted_hosts,
            operation="docker_executor.run",
        )

        if "docker_host" in execution.metadata:
            h = execution.metadata["docker_host"]
            p = execution.metadata["docker_port"]
            host, port, cl = self.pool.get_client(self, task.task_id, h, p)
        else:
            if blacklisted_hosts is None:
                blacklisted_hosts = self.get_blacklisted_hosts()
            host, port, cl = self.pool.get_client(
                self, task.task_id, blacklist=blacklisted_hosts
            )
            execution.metadata["docker_host"] = host
            execution.metadata["docker_port"] = port
            logger.warn(
                "Docker Host and Port were not found in the execution metadata. "
                "This is weird, since the update image part of running a job "
                "should have selected a docker host.",
                new_host=host,
                new_port=port,
            )

        logger = logger.bind(host=host, port=port)

        circuit = self.get_circuit(f"{host}:{port}")

        @circuit
        def run(logger):
            try:
                container_name = f"{job_prefix}-{execution.execution_id}"
                envs = job.metadata.get("envs", {})
                logger = logger.bind(container_name=container_name, envs=envs)
                logger.debug("Running the Job in Docker Host...")
                container = cl.containers.run(
                    image=f"{image}:{tag}",
                    name=container_name,
                    command=command,
                    detach=True,
                    environment=envs,
                )
                execution.metadata["container_id"] = container.id
                logger.info(
                    "Container started successfully. Container ID stored as Job Execution metadata.",
                    container_id=container.id,
                )
            except requests.exceptions.ConnectionError as err:
                error = traceback.format_exc()
                logger.error(
                    "Failed to connect to Docker Host. Will retry job later with a new host.",
                    error=error,
                )

                if "docker_host" in execution.metadata:
                    del execution.metadata["docker_host"]

                if "docker_port" in execution.metadata:
                    del execution.metadata["docker_port"]

                raise HostUnavailableError(host, port, err)

        run(logger)

        return True

    def stop_job(self, task, job, execution):
        logger = self.logger.bind(
            task_id=task.task_id,
            job_id=str(job.job_id),
            execution_id=str(execution.execution_id),
            operation="docker_executor.stop_job",
        )

        if "container_id" not in execution.metadata:
            logger.warn(
                "Can't stop Job Execution, since it has not been started. Aborting..."
            )

            return

        h = execution.metadata["docker_host"]
        p = execution.metadata["docker_port"]
        host, port, cl = self.pool.get_client(self, task.task_id, h, p)

        logger = logger.bind(host=host, port=port)

        circuit = self.get_circuit(f"{host}:{port}")

        @circuit
        def run(logger):
            try:
                container_id = execution.metadata["container_id"]
                logger = logger.bind(container_id=container_id)
                logger.debug("Finding container...")
                container = cl.containers.get(container_id)
                logger.info("Container found.")
                logger.debug("Stopping container...")
                container.stop()
                logger.info("Container stopped.")
            except requests.exceptions.ConnectionError as err:
                error = traceback.format_exc()
                logger.error("Failed to connect to Docker Host.", error=error)

                raise HostUnavailableError(host, port, err)

        run(logger)

    def convert_date(self, dt):
        return parse(dt)

    def get_result(self, task, job, execution):
        h = execution.metadata["docker_host"]
        p = execution.metadata["docker_port"]
        host, port, cl = self.pool.get_client(self, task.task_id, h, p)

        logger = self.logger.bind(
            task_id=task.task_id,
            job_id=str(job.job_id),
            execution_id=str(execution.execution_id),
            operation="docker_executor.get_result",
        )

        circuit = self.get_circuit(f"{host}:{port}")

        @circuit
        def run(logger):
            try:
                container_id = execution.metadata["container_id"]
                logger = logger.bind(container_id=container_id)
                logger.debug("Finding container...")
                container = cl.containers.get(container_id)
                logger.info("Container found.")

                return container

            except requests.exceptions.ConnectionError as err:
                raise HostUnavailableError(host, port, err)

        container = run(logger)

        # container.attrs['State']
        # {'Status': 'exited', 'Running': False, 'Paused': False, 'Restarting': False,
        # 'OOMKilled': False, 'Dead': False, 'Pid': 0, 'ExitCode': 0, 'Error': '',
        # 'StartedAt': '2018-08-27T17:14:14.1951232Z', 'FinishedAt': '2018-08-27T17:14:14.2707026Z'}

        container_id = execution.metadata["container_id"]
        logger = logger.bind(container_id=container_id)

        result = ExecutionResult(
            STATUS.get(container.status, ExecutionResult.Status.done)
        )

        state = container.attrs["State"]
        result.exit_code = state["ExitCode"]
        result.error = state["Error"]
        result.started_at = self.convert_date(state["StartedAt"])

        logger = logger.bind(
            status=container.status,
            state=state,
            exit_code=result.exit_code,
            error=result.error,
        )

        logger.debug("Container result found.")

        if (
            result.status == ExecutionResult.Status.done
            or result.status == ExecutionResult.Status.failed
        ):
            result.finished_at = self.convert_date(state["FinishedAt"])
            result.log = container.logs(stdout=True, stderr=False)

            if result.error != "":
                result.error += (
                    f"\n\nstderr:\n{container.logs(stdout=False, stderr=True)}"
                )
            else:
                result.error = container.logs(stdout=False, stderr=True)

            logger.info("Container finished executing.", finished_at=result.finished_at)

        return result

    def _get_all_clients(self, regex):
        clients = self.pool.clients.values()

        if regex is not None:
            for r, cl in self.pool.clients_per_regex:
                if r is not None and r != regex:
                    continue

                clients = cl

                break

        return [
            (host, port, client, self.get_circuit(f"{host}:{port}"))

            for host, port, client in clients
        ]

    def _list_containers(self, host, port, client, circuit):
        @circuit
        def run():
            running = []
            containers = client.containers.list(
                sparse=False, filters={"status": "running"}
            )

            for container in containers:
                if not container.name.startswith(job_prefix):
                    continue
                running.append((host, port, container.id))

            return running

        return run()

    def get_running_containers(self, regex=None, blacklisted_hosts=None):
        if blacklisted_hosts is None:
            blacklisted_hosts = self.get_blacklisted_hosts()

        running = []

        unavailable_clients = []
        unavailable_clients_set = set()
        clients = self._get_all_clients(regex)

        for (host, port, client, circuit) in clients:
            if f"{host}:{port}" in blacklisted_hosts:
                unavailable_clients_set.add(f"{host}:{port}")
                unavailable_clients.append(
                    (host, port, RuntimeError("server is blacklisted"))
                )

                continue

            try:
                running += self._list_containers(host, port, client, circuit)
            except Exception as err:
                unavailable_clients_set.add(f"{host}:{port}")
                unavailable_clients.append((host, port, err))

        return {
            "available": [
                {
                    "host": host,
                    "port": port,
                    "available": True,
                    "blacklisted": f"{host}:{port}" in blacklisted_hosts,
                    "circuit": circuit.current_state,
                    "error": None,
                }

                for (host, port, client, circuit) in clients

                if f"{host}:{port}" not in unavailable_clients_set
            ],
            "unavailable": [
                {
                    "host": host,
                    "port": port,
                    "available": False,
                    "blacklisted": f"{host}:{port}" in blacklisted_hosts,
                    "circuit": self.get_circuit(f"{host}:{port}").current_state,
                    "error": str(err),
                }

                for (host, port, err) in unavailable_clients
            ],
            "running": running,
        }

    def get_current_logs(self, task_id, job, execution):
        h = execution.metadata["docker_host"]
        p = execution.metadata["docker_port"]
        host, port, cl = self.pool.get_client(self, task_id, h, p)

        container_id = execution.metadata["container_id"]
        container = cl.containers.get(container_id)

        log = container.logs(stdout=True, stderr=True).decode("utf-8")

        return log

    def get_streaming_logs(self, task_id, job, execution):
        h = execution.metadata["docker_host"]
        p = execution.metadata["docker_port"]
        host, port, cl = self.pool.get_client(self, task_id, h, p)

        container_id = execution.metadata["container_id"]

        logger = self.logger.bind(
            task_id=task_id,
            job=str(job.job_id),
            execution_id=str(execution.execution_id),
            operation="docker_host.get_streaming_logs",
            host=host,
            port=port,
            container_id=container_id,
        )

        try:
            container = cl.containers.get(container_id)

            for log in container.logs(stdout=True, stderr=True, stream=True):
                yield log.decode("utf-8")
        except requests.exceptions.ConnectionError as err:
            error = traceback.format_exc()
            logger.error("Failed to connect to Docker Host.", error=error)

            raise HostUnavailableError(host, port, err)

    def get_blacklisted_hosts(self):
        redis = current_app.redis
        hosts = redis.smembers(blacklist_key)

        return set([host.decode("utf-8") for host in hosts])

    def mark_as_done(self, task, job, execution):
        h = execution.metadata["docker_host"]
        p = execution.metadata["docker_port"]
        host, port, cl = self.pool.get_client(self, task.task_id, h, p)

        container_id = execution.metadata["container_id"]

        logger = self.logger.bind(
            task_id=task.task_id,
            job=str(job.job_id),
            execution_id=str(execution.execution_id),
            operation="docker_host.mark_as_done",
            host=host,
            port=port,
            container_id=container_id,
        )

        try:
            logger.debug("Finding container...")
            container = cl.containers.get(container_id)
            logger.info("Container found.")

            new_name = f"defunct-{container.name}"
            logger.debug("Renaming container...", new_name=new_name)
            container.rename(new_name)
            logger.debug("Container renamed.", new_name=new_name)
        except requests.exceptions.ConnectionError as err:
            error = traceback.format_exc()
            logger.error("Failed to connect to Docker Host.", error=error)

            raise HostUnavailableError(host, port, err)

    def remove_done(self):
        removed_containers = []
        clients = self.pool.clients.values()

        for (host, port, client) in clients:
            containers = client.containers.list(
                sparse=False, all=True, filters={"name": f"defunct-{job_prefix}"}
            )

            for container in containers:
                removed_containers.append(
                    {
                        "host": f"{host}:{port}",
                        "name": container.name,
                        "id": container.id,
                        "image": container.image.attrs["RepoTags"][0],
                    }
                )
                container.remove()

        self.logger.info(
            "Removed all defunct containers.", removed_containers=removed_containers
        )

        return removed_containers
