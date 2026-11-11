# Test image for the installer bats suite (tests/install/). Debian userland on
# purpose: the installers target Debian 12. procps is required because bats
# implements BATS_TEST_TIMEOUT with pkill/ps and runs zero tests without them.
#
#   docker build -t jp-bats:latest -f tests/install/Dockerfile tests/install
#   docker run --rm -v "$PWD:/code" jp-bats:latest
FROM debian:bookworm-slim
RUN apt-get update -qq \
 && apt-get install -y -qq --no-install-recommends \
      shellcheck bats git python3 ca-certificates procps \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /code
CMD ["tests/install/run.sh"]
