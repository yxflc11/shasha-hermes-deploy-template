# Guizang Swiss seed provenance

- Upstream: `https://github.com/op7418/guizang-social-card-skill`
- Locked commit: `cf4b810fac1c73fb65a2bb31d8c9278d82cbc4c5`
- License: AGPL-3.0; preserved in `LICENSE`
- Upstream copyright: © 2026 op7418
- Vendored files:
  - `template-swiss-card.html` — SHA-256 `12ed65272b38c3779e422a56a3cde1fafce013249a6449c3195ac1773f12a7cb`
  - `validate-social-deck.mjs` — SHA-256 `ddad5dc54e0f16c35fe4bab6e2697db5894ea7064199b52a6b98287dcfde464e`
  - `LICENSE` — SHA-256 `8d56b405468aad11f87ab5763f901e276e08d9646ff5c8481b1762b6b789e9ed`

The global Codex Skill is never modified. Each brief HTML is compiled from the
vendored Swiss seed, with IKB Blue as the only accent. The runtime renderer has
no agent or image-model dependency and strips all remote font/icon requests.

Runtime base image: `mcr.microsoft.com/playwright:v1.60.0-noble`, locked to
`sha256:9bd26ad900bb5e0f4dee75839e957a89ae89c2b7ab1e76050e559790e946b948`.
The image's local WenQuanYi Zen Hei font is used for Chinese; the brief layout
uses numbered geometry and therefore needs no runtime icon library.

Do not add this third-party Skill to any Hermes messaging Skill allowlist. Hermes
only sees the audited ACT interaction Skills and the rendered brief manifest.
