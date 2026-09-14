import json
import os
import time
from pathlib import Path

import docker
import pytest
import requests
from docker.errors import ImageNotFound

# Testcontainers normally starts a helper container ("Ryuk") that cleans up
# after the test. Ryuk is pulled from Docker Hub, which our runners cannot
# reach. We clean up ourselves in the finally block, so Ryuk is switched off.
# This line MUST come before the testcontainers import, or it is ignored.
os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

from testcontainers.core.container import DockerContainer  # noqa: E402


def setting(name, convert=str):
    """Read one input from the environment. Fail with a plain message if it
    is missing or the wrong type, instead of a confusing Python traceback."""
    value = os.environ.get(name)
    if value is None:
        pytest.fail(f"Missing required input: {name}")
    try:
        return convert(value)
    except ValueError as error:
        pytest.fail(f"Input {name} is not valid: {error}")


def test_application_health():
    # ---- 1. Read inputs -----------------------------------------------------
    image = setting("TEST_IMAGE")
    port = setting("CONTAINER_PORT", int)
    health_path = setting("HEALTH_PATH")
    expected_status = setting("EXPECTED_STATUS", int)
    timeout = setting("HEALTH_TIMEOUT", float)
    interval = setting("HEALTH_INTERVAL", float)
    environment = setting("CONTAINER_ENV_JSON", json.loads)
    report_directory = Path(setting("REPORT_DIRECTORY"))

    # Optional inputs. Leave them unset and the test behaves as before.
    base_image = os.environ.get("BASE_IMAGE")
    # "or" (not a .get default) so an empty action input keeps the defaults.
    error_patterns = (
        os.environ.get("LOG_ERROR_PATTERNS")
        or "Traceback|SEVERE|startup failed|Cannot find module|Permission denied"
    ).split("|")

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

    # ---- 2. Check the images ------------------------------------------------
    # Require the image produced or explicitly pulled by the caller.
    # This prevents silently pulling an unintended image if it is missing.
    client = docker.from_env()
    try:
        try:
            app = client.images.get(image)
        except ImageNotFound:
            pytest.fail(f"Image {image} is not on this runner. Build or pull it first.")
        image_id = app.id
        print(f"Testing image: {image}")
        print(f"Local image ID: {image_id}")

        # Prove the app image was really built on top of the candidate base.
        # An image is a stack of layers, and a child image starts with exactly
        # the same layers as its parent. If the first layers do not match,
        # the build used a cached or different base, and passing this test
        # would prove nothing about the candidate.
        if base_image:
            try:
                base = client.images.get(base_image)
            except ImageNotFound:
                pytest.fail(f"Base image {base_image} is not on this runner.")
            base_layers = base.attrs["RootFS"]["Layers"]
            app_layers = app.attrs["RootFS"]["Layers"]
            if app_layers[: len(base_layers)] != base_layers:
                pytest.fail(
                    f"{image} was not built from {base_image}. "
                    "Check the build used the candidate tag with pull and no-cache on."
                )
            print(f"Confirmed base image: {base_image}")
    finally:
        client.close()

    # ---- 3. Start the container and wait for it to answer -------------------
    container = DockerContainer(image).with_exposed_ports(port)

    for name, value in environment.items():
        container.with_env(name, value)

    # Everything we learn is kept here and written to a file at the end, so
    # the workflow can report a reason without parsing the console log.
    result = {
        "image": image,
        "image_id": image_id,
        "passed": False,
        "reason": "Test did not complete",
    }

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
        healthy = False

        with requests.Session() as session:
            # Local container traffic should not use a corporate HTTP proxy.
            session.trust_env = False

            while time.monotonic() < deadline:
                running_container = container.get_wrapped_container()
                running_container.reload()

                if running_container.status != "running":
                    result["reason"] = (
                        "Application container stopped before becoming healthy. "
                        f"State: {running_container.status}"
                    )
                    pytest.fail(result["reason"])

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
                        healthy = True
                        break

                except requests.RequestException as error:
                    last_result = type(error).__name__

                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(min(interval, remaining))

        if not healthy:
            result["reason"] = (
                f"Application did not return HTTP {expected_status} "
                f"within {timeout:g} seconds. Last result: {last_result}"
            )
            pytest.fail(result["reason"])

        # ---- 4. It answers. Did it complain while starting? -----------------
        # An app can return 200 on /health while a whole part of it failed to
        # load. The health check cannot see that; the startup log can.
        stdout, stderr = container.get_logs()
        logs = (stdout + stderr).decode(errors="replace")
        found = [pattern for pattern in error_patterns if pattern and pattern in logs]
        if found:
            result["reason"] = f"Application is serving but logged startup errors: {found}"
            pytest.fail(result["reason"])

        result["passed"] = True
        result["reason"] = f"Application healthy ({last_result}), no startup errors logged"

    finally:
        # ---- 5. Save evidence, then clean up --------------------------------
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

        # Exit code and OOMKilled never appear in the logs. Without this, an
        # app killed for running out of memory looks like it simply hung.
        try:
            wrapped = container.get_wrapped_container()
            if wrapped is not None:
                wrapped.reload()
                state = wrapped.attrs["State"]
                (report_directory / "container-state.json").write_text(
                    json.dumps(state, indent=2)
                )
                print(
                    f"Container state: ExitCode={state.get('ExitCode')} "
                    f"OOMKilled={state.get('OOMKilled')} Error={state.get('Error')!r}"
                )

        except Exception as error:
            print(f"Could not collect container state: {type(error).__name__}")

        # Write the result so the workflow can read the reason.
        try:
            (report_directory / "smoke-result.json").write_text(
                json.dumps(result, indent=2)
            )
            github_output = os.environ.get("GITHUB_OUTPUT")
            if github_output:
                with open(github_output, "a") as handle:
                    handle.write(f"reason={result['reason']}\n")

        except Exception as error:
            print(f"Could not write smoke result: {type(error).__name__}")

        # A startup failure may mean no application container exists yet.
        if container.get_wrapped_container() is not None:
            container.stop()