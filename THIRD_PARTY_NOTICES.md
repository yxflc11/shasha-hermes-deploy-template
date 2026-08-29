# Third-party notices

This repository contains integrations, patches, dependencies, and one vendored visual seed. The names below identify upstream ownership; they do not imply endorsement.

## Guizang Social Card Skill

- Upstream: <https://github.com/op7418/guizang-social-card-skill>
- Locked commit: `cf4b810fac1c73fb65a2bb31d8c9278d82cbc4c5`
- Copyright: © 2026 op7418
- License: GNU Affero General Public License v3.0
- Vendored scope: the Swiss HTML seed, validation script, and full license under `deploy/hermes-ai-brief/third_party/guizang-social-card-skill/`

The local renderer is based on that vendored seed. The repository therefore uses AGPL-3.0-only for the combined public work. Exact vendored hashes and modification boundaries are recorded in the adjacent `UPSTREAM.md`.

## Hermes Agent

- Upstream: <https://github.com/NousResearch/hermes-agent>
- Referenced release: Hermes 0.20.6 / `v2026.8.27`, commit `5fc308a70719a83cccdbba4c0e39c23f5a8239d5`
- Copyright: Copyright (c) 2025 Nous Research
- License: MIT

Hermes core is not vendored. This repository contains configuration, companion components, and source-hash-guarded patches designed to be applied to that upstream release.

MIT notice for Hermes Agent:

> Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”), to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to inclusion of the upstream copyright and permission notice. The Software is provided “as is”, without warranty of any kind.

Consult the complete upstream MIT license before redistributing Hermes itself.

## Playwright

- Upstream: <https://github.com/microsoft/playwright>
- Locked dependency: Playwright 1.60.0
- Copyright: Microsoft Corporation and contributors
- License: Apache License 2.0

Playwright source is not vendored. The renderer package and Dockerfile install or reference the upstream runtime. Installed packages and container contents retain their own notices and license terms.

## External products and services

Telegram, Weixin/微信, Obsidian, Docker, GitHub, and AIHOT names are used only to describe interoperability. Their software, services, trademarks, data, and terms remain under their respective owners.
