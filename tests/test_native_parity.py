"""Docker-image / native-installer parity gates (native-install plan U4, R13).

The two distribution targets are built from the same tree and must agree on
the runtime contract: apt packages, the PyGObject pin, and the environment
variable names. `install-native.sh` declares its sets in plain shell
variables so this test can regex them out, the same way
tests/test_packaging_multiroom.py reads the Dockerfile.

Parity means: Dockerfile runtime set == RUNTIME_PKGS ∪ AIRPLAY_LIBS (the
AirPlay support libraries are installed only on architectures with a vendored
binary); Dockerfile builder set ⊆ BUILD_PKGS (the native build also compiles
CPython and the no-wheel C extensions); pins identical; every Dockerfile ENV
name appears in the installer's env template (containment, not equality — the
template is a superset because the rest are `-e` flags in Docker).

R14 ("Dockerfile untouched") is enforced by the Verification Contract's
empty-diff gate, not here: a hash constant would fail every future legitimate
Dockerfile edit and differ between CRLF and LF checkouts.
"""

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def _read(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


# ── Dockerfile side ──────────────────────────────────────────────────────────


_APT_FLAGS = {"-y", "-qq", "-q", "--no-install-recommends", "install"}


def _apt_blocks(dockerfile: str) -> list[set[str]]:
    """Every `apt-get install` occurrence's package set, in file order — single-line
    RUNs and backslash-continued blocks alike (a package added in a new RUN form must
    not slip past the gate). Continuations are joined first; tokens run from
    `install` to the next `&&`, `;` or end of command."""
    joined = re.sub(r"\\\n", " ", dockerfile)
    blocks = []
    for line in joined.splitlines():
        if line.lstrip().startswith("#"):
            continue  # prose about apt in comments is not an install
        for m in re.finditer(r"apt-get install\b([^&;]*)", line):
            pkgs = {tok for tok in m.group(1).split() if tok not in _APT_FLAGS and not tok.startswith("-")}
            if pkgs:
                blocks.append(pkgs)
    return blocks


def dockerfile_sets(dockerfile: str) -> tuple[set[str], set[str]]:
    """Builder set = every install block in the builder stage; runtime set = the union
    of every install block after the runtime `FROM`, so a package added in any RUN
    form in the runtime stage counts."""
    stages = re.split(r"^FROM .*$", dockerfile, flags=re.M)
    builder_src = next(s for s in stages if "pip install" in s and "hatchling" in s)
    runtime_src = next(s for s in stages if "gstreamer1.0-alsa" in s)
    builder = set().union(*_apt_blocks(builder_src)) if _apt_blocks(builder_src) else set()
    runtime = set().union(*_apt_blocks(runtime_src))
    assert builder and runtime, "expected builder and runtime apt packages"
    return builder, runtime


def dockerfile_env_names(dockerfile: str) -> set[str]:
    """Names from EVERY `ENV` instruction (a second ENV line must not be invisible)."""
    names: set[str] = set()
    for m in re.finditer(r"^ENV ((?:.*\\\n)*.*)$", dockerfile, re.M):
        names |= set(re.findall(r"([A-Z_]+)=", m.group(1)))
    assert names, "ENV block missing"
    return names


def dockerfile_pygobject_pin(dockerfile: str) -> str:
    m = re.search(r'pip install [^\n]*"(PyGObject[^"]*)"', dockerfile)
    assert m, "PyGObject pin missing from Dockerfile"
    return m.group(1)


# ── installer side ───────────────────────────────────────────────────────────


def shell_set(script: str, var: str) -> set[str]:
    m = re.search(rf'^{var}="\n((?:[^\n"]+\n)+)"', script, re.M)
    assert m, f"{var} multi-line set missing from install-native.sh"
    return set(m.group(1).split())


def shell_scalar(script: str, var: str) -> str:
    m = re.search(rf'^{var}="([^"]*)"', script, re.M)
    assert m, f"{var} missing from install-native.sh"
    return m.group(1)


@pytest.fixture(scope="module")
def dockerfile() -> str:
    return _read("Dockerfile")


@pytest.fixture(scope="module")
def installer() -> str:
    return _read("install-native.sh")


# ── parity assertions (the real tree) ────────────────────────────────────────


def assert_runtime_parity(dockerfile: str, installer: str) -> None:
    _, runtime = dockerfile_sets(dockerfile)
    native = shell_set(installer, "RUNTIME_PKGS") | shell_set(installer, "AIRPLAY_LIBS")
    missing = sorted(runtime - native)
    extra = sorted(native - runtime)
    assert not missing, f"Dockerfile runtime packages missing from install-native.sh: {missing}"
    assert not extra, f"install-native.sh runtime packages not in the Dockerfile: {extra}"


def test_runtime_package_parity(dockerfile, installer):
    assert_runtime_parity(dockerfile, installer)


def test_builder_packages_are_a_subset_of_build_pkgs(dockerfile, installer):
    builder, _ = dockerfile_sets(dockerfile)
    build = shell_set(installer, "BUILD_PKGS")
    missing = sorted(builder - build)
    assert not missing, f"Dockerfile builder packages missing from BUILD_PKGS: {missing}"


def test_airplay_libs_are_exactly_the_binaries_dependencies(installer):
    """The exclusion set must stay the AirPlay-only libraries, nothing else."""
    assert shell_set(installer, "AIRPLAY_LIBS") == {
        "libplist3", "libevent-2.1-7", "libevent-pthreads-2.1-7", "libcurl4", "libconfuse2",
    }


def test_pygobject_pin_identical(dockerfile, installer):
    assert shell_scalar(installer, "PYGOBJECT_PIN") == dockerfile_pygobject_pin(dockerfile)


def test_env_template_contains_every_dockerfile_env_name(dockerfile, installer):
    names = dockerfile_env_names(dockerfile)
    template = set(shell_scalar(installer, "ENV_TEMPLATE_VARS").split())
    missing = sorted(names - template)
    assert not missing, f"Dockerfile ENV names absent from the native env template: {missing}"
    assert {"DATA_DIR", "BIND_HOST", "PORT", "LOG_LEVEL", "JUKEPLOX_GIT_SHA"} <= names


def test_env_template_is_a_superset_not_equal(dockerfile, installer):
    """Guard against a future 'fix' that demands equality: the native-only
    JUKEPLOX_SNAPSERVER_CONF would then fail parity."""
    template = set(shell_scalar(installer, "ENV_TEMPLATE_VARS").split())
    assert "JUKEPLOX_SNAPSERVER_CONF" in template
    assert "JUKEPLOX_SNAPSERVER_CONF" not in dockerfile_env_names(dockerfile)


def test_badge_vars_match_build_info(installer):
    badge = set(shell_scalar(installer, "BADGE_VARS").split())
    src = _read("app/_build_info.py")
    assert badge == set(re.findall(r'os\.environ\.get\("(JUKEPLOX_[A-Z_]+)"', src))


# ── the gate must fail on drift (AE7) ────────────────────────────────────────


def test_extra_dockerfile_package_fails_and_is_named(dockerfile, installer):
    """Covers AE7: a package added to the Dockerfile only fails the gate and
    the message names it."""
    mutated = dockerfile.replace("    snapclient \\\n", "    snapclient \\\n    libfoo-bar9 \\\n", 1)
    assert mutated != dockerfile
    with pytest.raises(AssertionError) as exc:
        assert_runtime_parity(mutated, installer)
    assert "libfoo-bar9" in str(exc.value)


def test_missing_and_extra_installer_packages_are_named(dockerfile, installer):
    without = installer.replace("\nflac\n", "\n", 1)
    with pytest.raises(AssertionError) as exc:
        assert_runtime_parity(dockerfile, without)
    assert "flac" in str(exc.value)
    extra = installer.replace("\nflac\n", "\nflac\nlibextra-only\n", 1)
    with pytest.raises(AssertionError) as exc2:
        assert_runtime_parity(dockerfile, extra)
    assert "libextra-only" in str(exc2.value)


def test_single_line_run_install_is_seen(dockerfile, installer):
    """Review #11: a package added via a one-line RUN in the runtime stage fails parity."""
    mutated = dockerfile.rstrip("\n") + "\nRUN apt-get update && apt-get install -y --no-install-recommends libasound2-plugins && rm -rf /var/lib/apt/lists/*\n"
    with pytest.raises(AssertionError) as exc:
        assert_runtime_parity(mutated, installer)
    assert "libasound2-plugins" in str(exc.value)


def test_second_env_line_is_seen(dockerfile):
    """Review #11: a second ENV instruction contributes its names."""
    mutated = dockerfile.rstrip("\n") + "\nENV JUKEPLOX_EXTRA_FLAG=1\n"
    assert "JUKEPLOX_EXTRA_FLAG" in dockerfile_env_names(mutated)
    assert "JUKEPLOX_EXTRA_FLAG" not in dockerfile_env_names(dockerfile)


def test_pin_drift_by_one_character_fails(dockerfile, installer):
    drifted = installer.replace('PYGOBJECT_PIN="PyGObject<3.50"', 'PYGOBJECT_PIN="PyGObject<3.51"', 1)
    assert drifted != installer
    assert shell_scalar(drifted, "PYGOBJECT_PIN") != dockerfile_pygobject_pin(dockerfile)


def test_env_name_absent_from_template_is_named(dockerfile, installer):
    drifted = installer.replace("LOG_LEVEL ", "", 1)
    template = set(shell_scalar(drifted, "ENV_TEMPLATE_VARS").split())
    missing = sorted(dockerfile_env_names(dockerfile) - template)
    assert missing == ["LOG_LEVEL"]


# ── repo-inspectable installer facts ─────────────────────────────────────────


def test_installer_pins_both_downloads():
    s = _read("install-native.sh")
    assert re.search(r'^PY_SHA256="[0-9a-f]{64}"', s, re.M)
    for arch in ("i686", "x86_64", "aarch64"):
        assert re.search(rf'^RUSTUP_SHA256_{arch}="[0-9a-f]{{64}}"', s, re.M), arch
    assert "sh.rustup.rs" not in s


def test_installer_is_lf_and_sourceable():
    raw = (_ROOT / "install-native.sh").read_bytes()
    assert b"\r\n" not in raw, "install-native.sh must be LF (CRLF shebang does not execute)"
    assert b'JUKEPLOX_INSTALL_LIB' in raw
