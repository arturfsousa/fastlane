# Standard Library
from json import dumps
from uuid import uuid4

# 3rd Party
import pytest
from preggy import expect

# Fastlane
from fastlane.worker.docker_executor import blacklist_key


def test_docker_blacklist1(client):
    """Test blacklisting a docker server"""

    def ensure_blacklist(method):
        docker_host = str(uuid4())

        data = {"host": docker_host}
        rv = getattr(client, method)(
            "/docker-executor/blacklist", data=dumps(data), follow_redirects=True
        )

        expect(rv.status_code).to_equal(200)
        expect(rv.data).to_be_empty()

        app = client.application

        res = app.redis.exists(blacklist_key)
        expect(res).to_be_true()

        res = app.redis.sismember(blacklist_key, docker_host)
        expect(res).to_be_true()

    for method in ["post", "put"]:
        ensure_blacklist(method)


def test_docker_blacklist2(client):
    """
    Test blacklisting a docker server with invalid body or
    without a host property in the JSON body
    """

    pytest.skip("Not implemented")


def test_docker_blacklist3(client):
    """Test removing from blacklist a docker server"""
    docker_host = str(uuid4())

    data = {"host": docker_host}
    rv = client.post(
        "/docker-executor/blacklist", data=dumps(data), follow_redirects=True
    )

    expect(rv.status_code).to_equal(200)
    expect(rv.data).to_be_empty()

    app = client.application

    res = app.redis.exists(blacklist_key)
    expect(res).to_be_true()

    res = app.redis.sismember(blacklist_key, docker_host)
    expect(res).to_be_true()

    data = {"host": docker_host}
    rv = client.delete(
        "/docker-executor/blacklist", data=dumps(data), follow_redirects=True
    )

    expect(rv.status_code).to_equal(200)
    expect(rv.data).to_be_empty()

    app = client.application

    res = app.redis.exists(blacklist_key)
    expect(res).to_be_false()


def test_docker_blacklist4(client):
    """
    Test removing a server from blacklist with invalid body or
    without a host property in the JSON body
    """

    pytest.skip("Not implemented")
