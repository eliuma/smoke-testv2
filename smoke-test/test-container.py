import json
import os
import time
from pathlib import Path

import docker
import pytest
import requests
from testcontainers.core.container import DockerContainer


def test_application_health():
    image = os.environ["TEST_IMAGE"]
    port = int(os.environ["CONTAINER_PORT"])
    health_path = os.environ["HEALTH_PATH"]
    expected_status = int(os.environ["EXPECTED_STATUS"])
    timeout = float(os.environ["HEALTH_TIMEOUT"])
    interval = float(os.environ["HEALTH_INTERVAL"])
    environment = json.loads(os.environ["CONTAINER_ENV_JSON"])

    report_directory = Path(os.environ["REPORT_DIRECTORY"])
    report_directory.mkdir(parents=True, exist_ok=True)

    assert 1 <= port <= 65535, "Invalid container port"
    assert health_path.startswith("/"), "health-path must start with /"
    assert timeout > 0, "timeout must be positive"
    assert interval > 0, "interval must be positive"
    assert isinstance(environment, dict), "container-env must be a JSON object"
    assert all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in environment.items()
    ), "Container environment names and values must be strings"

    # Require the image produced or explicitly pulled by the caller.
    # This prevents silently pulling an unintended image if it is missing.
    client = docker.from_env() 
    try:
        built_image = client.images.get(image)
        print(f"Testing image: {image}")
        print(f"Local image ID: {built_image.id}")
    finally:
        client.close()

    container = DockerContainer(image).with_exposed_ports(port)

    for name, value in environment.items():
        container.with_env(name, value)

    try:
        container.start()

        host = container.get_container_host_ip()
        mapped_port = container.get_exposed_port(port)

        # Support an IPv6 host address if one is returned.
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"

        url = f"http://{host}:{mapped_port}{health_path}"
        print(f"Checking endpoint: {url}")

        deadline = time.monotonic() + timeout
        last_result = "No HTTP response received"

        with requests.Session() as session:
            # Local container traffic should not use a corporate HTTP proxy.
            session.trust_env = False

            while time.monotonic() < deadline:
                running_container = container.get_wrapped_container()
                running_container.reload()

                if running_container.status != "running":
                    pytest.fail(
                        "Application container stopped before becoming healthy. "
                        f"State: {running_container.status}"
                    )

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                try:
                    response = session.get(
                        url,
                        timeout=min(5.0, remaining),
                        allow_redirects=False,
                    )

                    last_result = f"HTTP {response.status_code}"

                    if response.status_code == expected_status:
                        print(f"Health check passed: {last_result}")
                        return

                except requests.RequestException as error:
                    last_result = type(error).__name__

                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(interval, remaining))

        pytest.fail(
            f"Application did not return HTTP {expected_status} "
            f"within {timeout:g} seconds. Last result: {last_result}"
        )

    finally:
        # Capture logs before removing the container, including on failure.
        try:
            stdout, stderr = container.get_logs()

            (report_directory / "container-stdout.log").write_bytes(stdout)
            (report_directory / "container-stderr.log").write_bytes(stderr)

            print("::group::Application container logs")
            print(stdout.decode(errors="replace"))
            print(stderr.decode(errors="replace"))
            print("::endgroup::")

        except Exception as error:
            print(f"Could not collect container logs: {type(error).__name__}")

        finally:
            # A startup failure may mean no application container exists yet.
            if container.get_wrapped_container() is not None:
                container.stop()