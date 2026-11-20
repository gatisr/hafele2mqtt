# Deploying with scripts/deploy.sh

Back to the [README](../README.md).

`scripts/deploy.sh` builds this checkout into an image, tags it with the version and with `latest`, brings up only this service on the Docker target you configured, then asks the running container which version it actually is before publishing that to Home Assistant as `sensor.hafele2mqtt_deployed_version` and reading it back.

Its settings live in `.env.deploy`, not in `.env`, because `.env` is handed to the container as its runtime environment and a Home Assistant token has no business being in there. Copy `.env.deploy.example` to `.env.deploy` and fill in `HA_URL`, `HA_TOKEN` and exactly one Docker target; `IMAGE`, `COMPOSE_FILE`, `SERVICE` and `PLATFORM` all have working defaults. Every key can also be passed as an environment variable, which wins over the file, and `.env.deploy` is gitignored.

Neither credential is ever put on a command line. `ps` shows the full argument list of every process to every user on the machine, so an `Authorization` header passed to `curl` as `-H` is readable by anyone logged in for as long as the call runs; both headers go to `curl` through a config file on its standard input instead. A credential containing a line break is rejected when it is read, because it cannot be expressed in that file and is a copy-paste accident every time.

```sh
scripts/deploy.sh --dry-run       # print every step, build nothing, change nothing
scripts/deploy.sh                 # build, start the service, publish the version
scripts/deploy.sh --version-only  # publish the version sensor and nothing else
```

`--no-reload` builds and tags the image but leaves the running container alone, `--force` deploys from a working tree with uncommitted changes, and `-h` lists everything. The script refuses to guess a Docker target, because deploying to the wrong daemon is the easiest mistake here: set `DOCKER_CONTEXT=default` if you mean the local one.

There are three ways to name that target, and they are mutually exclusive:

| Setting | When to use it |
| --- | --- |
| `DOCKER_CONTEXT` | A daemon the Docker CLI already knows, by context name |
| `DOCKER_HOST` | A daemon by endpoint, for example `ssh://pi@192.0.2.10` |
| `DOCKER_API_URL` | A Docker Engine API over HTTP that the CLI cannot be pointed at |

`DOCKER_API_URL` exists for a daemon that is only reachable through a proxy of its own, which usually wants an extra header: put it in `DOCKER_API_HEADER` as `Name: value`. In this mode the image is built on the machine running the script, exported with `docker save`, POSTed to the target's `/images/load`, and read back from `/images/<image>:<tag>/json` before anything is started. The target builds nothing, so set `PLATFORM` to its architecture, for example `linux/arm64`.

A loaded image still has to be started by someone, and over the API that someone is not compose. `DEPLOY_CMD` replaces `docker compose up -d` with whatever does own the stack, be it a deploy API, an Ansible run or an `ssh` command. It is run with `IMAGE`, `TAG` and `SERVICE` in its environment and a non-zero exit stops the deploy. The version check afterwards still happens: the container is found by its `com.docker.compose.service` label, falling back to its name, and asked for its version over the API.

One caveat worth knowing: an entity created through the Home Assistant REST API is not persistent. It lives in the running state machine only, so the next Home Assistant restart forgets it until the next deploy recreates it. That is fine for a deploy marker, but do not build automations on it.
