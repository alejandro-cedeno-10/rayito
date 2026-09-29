## MODIFIED Requirements

### Requirement: Image ships the kernel stack and a warm snapshot
The `rayito-base` image SHALL install the exact pins of `kernel-sidecar/requirements.txt` (`ipykernel==7.3.0`, `ipython==9.17.1`, `jupyter_client==8.10.0`, `pyzmq==27.2.0`, `matplotlib==3.11.2`, `pandas==3.0.6`, `numpy==2.5.3`, plus the resolved exact `scipy` and `scikit-learn`) for Python 3.12 from wheels, copy the sidecar to `/opt/rayito/sidecar` (readable by all, `tests`/caches/`requirements.in` excluded, `requirements-poly.txt` included), build the matplotlib font cache and the IPython profile as `user` at build time, and take the snapshot only after `/ready` reported a warm default kernel. The same `Dockerfile` SHALL contain one conditional layer that runs only when `/opt/rayito/sidecar/kernels_variant` holds `poly` (installing the pins of `kernel-sidecar/requirements-poly.txt` with `pip check`, verifying `import bash_kernel` as `user` and the `rayito-bash` template, and installing the Deno 2.9.7 `aarch64-unknown-linux-gnu` binary, verified against its pinned sha256, at `/opt/rayito/deno/deno` with the `rayito-javascript` and `rayito-typescript` templates checked; no Node.js, `ijavascript` or compiler, Q57, Q61), so that the `full` artifact installs nothing new. `image-publish` SHALL record `snapshotBuild` sizes and build time; a `rayito-base` rebuilt from this Dockerfile SHALL report `memorySnapshotSizeInBytes` within 20 MB and `codeInstallSizeInBytes` (net of the `rayd` binary size change) within 10 MB of the previous version. The warm-up import list SHALL be decided by the measured rule of the `image-lifecycle` capability (`scipy.stats` and `sklearn.linear_model` leave the warm-up iff their combined RSS delta exceeds 100 MB; `numpy`, `pandas` and `matplotlib.pyplot` always stay because `/validate` and the acceptance tests use them), replacing the former 1.2 GB trimming knob. `requirements.txt` SHALL be reproducible from a committed `requirements.in` (the direct dependencies) with `uv pip compile --only-binary :all: --python-platform aarch64-manylinux_2_28 --python-version 3.12`.

#### Scenario: font cache present in the snapshot
- **WHEN** the SDK runs `import matplotlib, glob, os; sorted(os.path.basename(p) for p in glob.glob(os.path.join(matplotlib.get_cachedir(), 'fontlist-*.json')))` in a sandbox
- **THEN** the result lists at least one `fontlist-*.json`

#### Scenario: sizes reported
- **WHEN** `make image-publish` finishes the M6 version
- **THEN** the log prints `memorySnapshotSizeInBytes`, `codeInstallSizeInBytes`, `diskSnapshotSizeInBytes` and the build seconds, and the task notes record them next to version 10.0's

#### Scenario: packages stay importable after trimming
- **WHEN** the warm-up no longer imports `scipy` and `sklearn` and a user cell runs `import scipy.stats, sklearn.linear_model`
- **THEN** the cell succeeds

#### Scenario: rayito-base unchanged by the poly layer
- **WHEN** `rayito-base` is republished from the Dockerfile that carries the conditional layer and its `snapshotBuild` is compared with version 17.0 (`928 100 352` B memory)
- **THEN** `|memory delta| ≤ 20 MB`, `|code install delta net of the rayd binary| ≤ 70 MB` (10 MB in M7; from M9 on git-core, Q76, and the `rayd` growth since 17.0 are inside it), and a sandbox from it has no `bash_kernel` importable (the conditional layer was a no-op; the builder publishes no Dockerfile log)

#### Scenario: rayito-base carries no Deno
- **WHEN** the e2e runs `test -e /opt/rayito/deno` through `commands.run` on a sandbox of the `rayito-base` rebuilt from the Dockerfile with the Deno lines
- **THEN** the command exits non-zero (`CommandExitException`) and `run_code("1", language="typescript")` fails with `UNIMPLEMENTED` naming `rayito-base-poly`

#### Scenario: requirements.txt reproduces from requirements.in
- **WHEN** `uv pip compile --only-binary :all: --python-platform aarch64-manylinux_2_28 --python-version 3.12 kernel-sidecar/requirements.in -o /tmp/out.txt` runs
- **THEN** `/tmp/out.txt` matches `kernel-sidecar/requirements.txt` (module names normalised, no `# via` annotations)

#### Scenario: e2b/data resolves DataFrame and Series on pandas 2 and pandas 3
- **WHEN** a kernel executes `import pandas as pd; pd.DataFrame({'a': [1, 2]})` and the pandas pin is `2.2.3` (module `pandas.core.frame`) or `3.0.6` (module `pandas`)
- **THEN** the `execute_result` bundle contains `e2b/data` with the frame's columns either way
