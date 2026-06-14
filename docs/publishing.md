# Publishing & releasing

How this integration is distributed through HACS, and how to get it into the
HACS **default store**.

## Cutting a release

HACS installs the latest GitHub **release**. With no releases, it falls back to
the default branch and shows a commit SHA instead of a version — so always tag.

1. Bump `"version"` in `custom_components/junghome/manifest.json`.
2. Tag and push:
   ```sh
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
3. `.github/workflows/release.yml` then creates the GitHub release automatically
   (it fails if the tag doesn't match the manifest version, to keep them in sync).

The tag name (minus the leading `v`) is the version HACS offers.

### Beta / pre-release versions

To publish a version **without** auto-updating everyone, use a pre-release
version string — a suffix on the `X.Y.Z` base, e.g. `1.1.0b1` or `1.1.0-rc1`
(set it in `manifest.json` and tag it `v1.1.0b1`). `release.yml` detects the
suffix and marks the GitHub release as a **pre-release**. HACS hides pre-releases
unless a user enables **show beta versions** for the repo, so only opt-in testers
get it. Promote to stable later by releasing a plain `X.Y.Z`.

## Custom repository (works today)

Users add `https://github.com/ernetas/junghome` as a HACS custom repository,
category **Integration**. See the README. Nothing else required.

## Getting into the HACS default store

So users find "Jung Home" without adding a custom repo. Status:

Done:
- ✅ Public repo, MIT license, `custom_components/junghome/` layout
- ✅ `manifest.json` (`documentation`, `issue_tracker`, `codeowners`, `version`, `iot_class`)
- ✅ `hacs.json` with `name`
- ✅ Repo **description** and **topics** set
- ✅ A GitHub **release** (`v1.0.0`)
- ✅ **Brand icons** merged into home-assistant/brands and live at
  <https://brands.home-assistant.io/junghome/icon.png> (the `ignore: brands` line
  has been removed from `.github/workflows/validate.yml`)

Remaining, in order:

### 1. Make validation green

The `Validate` workflow runs hassfest + the HACS action on every push/PR to
`main`. Confirm both pass now that brands are live and the `ignore` is removed.

### 2. PR to hacs/default

Add the repo to the [hacs/default](https://github.com/hacs/default) list:

```sh
gh repo fork hacs/default --clone
cd default
# add "ernetas/junghome" to the `integration` file (JSON array), keeping it sorted
git checkout -b add-junghome
git commit -am "Add ernetas/junghome"
git push -u origin add-junghome
gh pr create --repo hacs/default --fill
```

The HACS bot validates the repo on the PR; brands must already be merged or it
fails. After the PR is merged, "Jung Home" appears in the default store.
