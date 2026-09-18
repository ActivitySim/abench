# Publishing abench

The package supports macOS and Linux hosts with Python 3.10 or newer. Docker is
an external prerequisite; ActivitySim and Sharrow run inside its Linux containers.
The console entry point allows `uvx abench experiments.yaml` once published.

## One-time PyPI setup

On the owning PyPI account's [Publishing page](https://pypi.org/manage/account/publishing/),
add a pending GitHub publisher with exactly these values:

| Field | Value |
| --- | --- |
| PyPI project name | `abench` |
| Owner | `ActivitySim` |
| Repository name | `abench` |
| Workflow name | `publish.yml` |
| Environment name | `pypi` |

Create the `pypi` environment in the GitHub repository settings. No PyPI API token
or GitHub Actions secret is needed. The first successful upload creates the PyPI
project; registering a pending publisher does not reserve the name. Configure
project owners/organization membership on PyPI as appropriate after creation.

See the official [pending publisher instructions](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).

## Release procedure

1. Update `__version__` in `src/abench/__init__.py`. Package metadata reads this
   value automatically; there is no second version field to synchronize.
2. Run pre-commit and the test suite. Review the code intended for the release.
3. Commit and push the release changes on a working branch, then merge through
   the repository's normal review process.
4. Create a GitHub release with tag `v<version>` targeting that reviewed commit
   (for example, `v0.1.0`). Publishing the release triggers `publish.yml`.
5. The workflow runs unit and Docker tests, builds and checks the sdist and wheel,
   verifies the tag, and runs an isolated `uvx` smoke test before uploading to PyPI.
6. Verify from outside the checkout:

   ```bash
   uvx abench@0.1.0 --version
   uvx abench@0.1.0 --help
   uvx abench@latest /absolute/path/to/experiments.yaml
   ```

For prereleases, use a PEP 440 version such as `0.1.0rc1` and tag `v0.1.0rc1`.
A published GitHub prerelease also triggers publishing; users should explicitly
request its version with `uvx abench@0.1.0rc1`.

PyPI release files cannot be replaced. If published code needs changing, release
a new version. A failed workflow can be rerun after fixing publisher configuration
if no distributions have been uploaded yet.
