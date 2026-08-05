# Third-party software

The production image contains the locked Python runtime dependencies from
`uv.lock` and the compiled Svelte application from `web/pnpm-lock.yaml`. The
source distributions and lockfiles remain the authoritative dependency lists.

## Runtime license families

| License | Runtime components |
| --- | --- |
| MIT | annotated-doc, annotated-types, anyio, click-default-group, fastapi, linkify-it-py, markdown-it-py, mdit-py-plugins, mdurl, platformdirs, pydantic, pydantic-core, pysilero-vad, rich, Svelte, textual, typing-inspection, uc-micro-py |
| BSD-3-Clause | click, h11, idna, starlette, uvicorn, websockets |
| BSD-2-Clause | Pygments |
| PSF-2.0 | typing-extensions |

The frontend build toolchain also contains packages under MIT, MIT-0,
Apache-2.0, BSD-2-Clause, BSD-3-Clause, ISC, and MPL-2.0. Those build-only
packages are not copied into the production image; their compiled output may
still carry applicable notices.

## Svelte notice

Svelte 5.56.8 is distributed under the MIT License:

> Copyright (c) 2016-2025 Svelte Contributors
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to deal
> in the Software without restriction, including without limitation the rights
> to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
> copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
> OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
> SOFTWARE.

## Reproducing the inventory

Build the locked development image, then inspect both dependency sets:

```bash
make dev-image
docker run --rm --init --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  --tmpfs /work-env:rw,exec,nosuid,size=1g \
  --user "$(id -u):$(id -g)" \
  -e HOME=/tmp -e UV_PROJECT_ENVIRONMENT=/work-env/venv \
  -v "$PWD:/workspace:ro" -w /workspace \
  2xbrainz-dev:local \
  sh -c 'uv sync --frozen --no-dev && uv pip list --python /work-env/venv/bin/python'
docker run --rm --init --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=512m \
  2xbrainz-dev:local \
  sh -c 'cd /opt/web && pnpm licenses list'
```

Review the license files shipped with each locked distribution before changing
or redistributing dependencies. This inventory is informational and is not
legal advice.
