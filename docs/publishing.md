# Publishing & releasing

How this integration is released and distributed. It is listed in the HACS
**default store**, so users install it by searching HACS — see
[Distribution](#distribution-hacs-default-store).

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

## Distribution: HACS default store

**Jung Home is in the [hacs/default](https://github.com/hacs/default) list**, so
users find it by searching HACS for "Jung Home" — no custom repository step. The
README documents the user-facing flow.

Adding a custom repository still works and is occasionally useful for testing an
unreleased branch: HACS -> ⋮ -> **Custom repositories**, repository
`https://github.com/ernetas/junghome`, category **Integration**.

### What getting there required (all done)

- Public repo, MIT license, `custom_components/junghome/` layout
- `manifest.json` with `documentation`, `issue_tracker`, `codeowners`,
  `version`, `iot_class`
- `hacs.json` with `name`
- Repo **description** and **topics** set
- A GitHub **release**
- **Brand icons** merged into home-assistant/brands, live at
  <https://brands.home-assistant.io/junghome/icon.png> (so the `ignore: brands`
  line is gone from `.github/workflows/validate.yml`)
- Green `Validate` workflow (hassfest + the HACS action) on every push/PR to
  `main`
- A merged PR adding `ernetas/junghome` to the `integration` list in
  [hacs/default](https://github.com/hacs/default)

### Staying in it

The HACS action keeps running in `Validate` on every push and PR. If it starts
failing, the listing is at risk — treat a red HACS check on `main` as urgent
rather than cosmetic. Note that the action talks to the HACS backend, so a
transient `Not Found` / "not loaded properly in HACS" is an infrastructure blip
rather than a repo defect; re-run before investigating.
