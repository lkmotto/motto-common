# PyPI Trusted Publishing for motto-common

## Overview

`motto-common` uses **PyPI Trusted Publishing** (OIDC) to publish releases
without long-lived API tokens.  GitHub Actions authenticates to PyPI via
OpenID Connect and pushes the built wheel directly.

## GitHub Actions Side (configured)

The [release workflow](../.github/workflows/release.yml) is already set up for
Trusted Publishing:

- **id-token permission**: The workflow declares `permissions: id-token: write`
  so GitHub can mint a short-lived OIDC token for PyPI.
- **Publisher action**: Uses `pypa/gh-action-pypi-publish@release/v1` which
  automatically exchanges the OIDC token for a PyPI API token.

```yaml
permissions:
  contents: write
  id-token: write          # <-- required for OIDC

# ...

- name: Publish to PyPI
  uses: pypa/gh-action-pypi-publish@release/v1
```

## PyPI Project Side (manual setup required)

You must register `lkmotto/motto-common` as a **Trusted Publisher** on PyPI.
This is a one-time manual step on [pypi.org](https://pypi.org):

1.  Go to **<https://pypi.org/manage/project/motto-common/settings/publishing/>**
2.  Under *Trusted Publisher Management*, fill in:
    - **Owner**: `lkmotto`
    - **Repository name**: `motto-common`
    - **Workflow name**: `release.yml`
    - **Environment name**: *(leave blank unless you use deployment environments)*
3.  Click **Add**.

Once registered, the next `git push --tags` that matches `v*` will
automatically publish a new release to PyPI.

## Verification

After setting up the Trusted Publisher on pypi.org, trigger a release:

```bash
git tag v0.1.0
git push origin v0.1.0
```

Watch the [Release workflow run](https://github.com/lkmotto/motto-common/actions/workflows/release.yml)
— the *Publish to PyPI* step should complete successfully.
