# 第三方依赖许可证清单

由 `uv run python scripts/check_licenses.py --write` 生成，不要手改。
Python 部分来自生成时的本机环境（Windows），Linux 上会多出或少几个平台相关的包。

规则：GPL、AGPL、SSPL 等强传染许可禁止使用；LGPL 需逐个确认；
认不出许可的包要人工核对后登记。
MPL-2.0 是文件级传染：只要不修改这些包的源码就可以使用。

| 生态 | 包 | 版本 | 许可 | 开发依赖 | 结论 |
|---|---|---|---|---|---|
| python | aeventkit | 2.1.0 | BSD License / Other/Proprietary License | — | 可用（已核对：随包 LICENSE 为 BSD-2-Clause，分类误标） |
| python | alembic | 1.20.1 | MIT | — | 可用 |
| python | annotated-doc | 0.0.5 | MIT | — | 可用 |
| python | annotated-types | 0.8.0 | MIT | — | 可用 |
| python | anyio | 4.15.1 | MIT | — | 可用 |
| python | ast_serialize | 0.13.1 | MIT | — | 可用 |
| python | boolean.py | 5.0 | BSD-2-Clause | — | 可用 |
| python | cachecontrol | 0.14.4 | Apache-2.0 | — | 可用 |
| python | certifi | 2026.10.23 | Mozilla Public License 2.0 (MPL 2.0) | — | 可用 |
| python | charset-normalizer | 3.5.2 | MIT | — | 可用 |
| python | click | 8.5.0 | BSD-3-Clause | — | 可用 |
| python | colorama | 0.4.6 | BSD License | — | 可用 |
| python | coverage | 7.16.2 | Apache-2.0 | — | 可用 |
| python | cyclonedx-python-lib | 11.12.0 | Apache Software License | — | 可用 |
| python | defusedxml | 0.7.1 | Python Software Foundation License | — | 可用 |
| python | duckdb | 1.5.6 | MIT License | — | 可用 |
| python | exchange_calendars | 4.13.2 | Apache-2.0 | — | 可用 |
| python | fastapi | 0.162.0 | MIT | — | 可用 |
| python | filelock | 4.2.8 | MIT | — | 可用 |
| python | greenlet | 3.5.6 | MIT AND PSF-2.0 | — | 可用 |
| python | grimp | 3.17 | BSD License | — | 可用 |
| python | h11 | 0.16.0 | MIT License | — | 可用 |
| python | httpcore | 1.0.9 | BSD-3-Clause | — | 可用 |
| python | httpx | 0.28.1 | BSD License | — | 可用 |
| python | hypothesis | 6.170.5 | MPL-2.0 | — | 可用 |
| python | ib_async | 2.1.0 | Other/Proprietary License | — | 可用（已核对：随包 LICENSE 为 BSD-2-Clause，分类误标） |
| python | idna | 3.20 | BSD-3-Clause | — | 可用 |
| python | import-linter | 2.15 | BSD License | — | 可用 |
| python | iniconfig | 2.3.1 | MIT | — | 可用 |
| python | jaraco.classes | 3.4.0 | MIT License | — | 可用 |
| python | jaraco.context | 6.1.2 | MIT | — | 可用 |
| python | jaraco.functools | 4.6.0 | MIT | — | 可用 |
| python | keyring | 25.7.0 | MIT | — | 可用 |
| python | korean_lunar_calendar | 0.4.0 | MIT | — | 可用 |
| python | librt | 0.16.0 | MIT | — | 可用 |
| python | license-expression | 30.4.4 | Apache-2.0 | — | 可用 |
| python | mako | 1.4.3 | MIT | — | 可用 |
| python | markdown-it-py | 4.2.0 | MIT License | — | 可用 |
| python | markupsafe | 3.0.4 | BSD-3-Clause | — | 可用 |
| python | mdurl | 0.1.2 | MIT License | — | 可用 |
| python | more-itertools | 11.2.1 | MIT | — | 可用 |
| python | msgpack | 1.2.4 | Apache-2.0 | — | 可用 |
| python | mypy | 2.4.0 | MIT | — | 可用 |
| python | mypy_extensions | 1.1.0 | MIT | — | 可用 |
| python | nest-asyncio | 1.6.0 | BSD License | — | 可用 |
| python | numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | — | 可用 |
| python | opentelemetry-api | 1.45.2 | Apache-2.0 | — | 可用 |
| python | packageurl-python | 0.17.6 | MIT License | — | 可用 |
| python | packaging | 26.3 | Apache-2.0 OR BSD-2-Clause | — | 可用 |
| python | pandas | 3.1.0 | BSD-3-Clause | — | 可用 |
| python | pandas-stubs | 3.0.5.260914 | BSD-3-Clause | — | 可用 |
| python | pathspec | 1.1.1 | Mozilla Public License 2.0 (MPL 2.0) | — | 可用 |
| python | pip | 26.3 | MIT | — | 可用 |
| python | pip-requirements-parser | 32.0.1 | MIT | — | 可用 |
| python | pip_api | 0.0.35 | Apache Software License | — | 可用 |
| python | pip_audit | 2.10.1 | Apache Software License | — | 可用 |
| python | platformdirs | 4.14.5 | MIT | — | 可用 |
| python | pluggy | 1.7.0 | MIT License | — | 可用 |
| python | py-serializable | 2.1.0 | Apache Software License | — | 可用 |
| python | pyarrow | 23.0.1 | Apache-2.0 | — | 可用 |
| python | pyarrow-stubs | 20.0.0.20260819 | BSD-2-Clause | — | 可用 |
| python | pydantic | 2.14.1 | MIT | — | 可用 |
| python | pydantic_core | 2.50.1 | MIT | — | 可用 |
| python | pygments | 2.21.0 | BSD-2-Clause | — | 可用 |
| python | pyluach | 2.3.0 | MIT License | — | 可用 |
| python | pyparsing | 3.3.3 | MIT | — | 可用 |
| python | pytest | 9.1.1 | MIT | — | 可用 |
| python | pytest-asyncio | 1.4.0 | Apache-2.0 | — | 可用 |
| python | pytest-cov | 7.1.0 | MIT | — | 可用 |
| python | python-dateutil | 3.0.0 | Apache Software License / BSD License | — | 可用 |
| python | pywin32-ctypes | 0.2.3 | BSD-3-Clause | — | 可用 |
| python | requests | 2.34.2 | Apache Software License | — | 可用 |
| python | rich | 15.0.0 | MIT License | — | 可用 |
| python | ruff | 0.17.1 | MIT | — | 可用 |
| python | six | 1.17.0 | MIT License | — | 可用 |
| python | sortedcontainers | 2.4.0 | Apache Software License | — | 可用 |
| python | sqlalchemy | 2.0.55 | MIT | — | 可用 |
| python | starlette | 1.10.0 | BSD-3-Clause | — | 可用 |
| python | tomli | 2.5.0 | MIT | — | 可用 |
| python | tomli_w | 1.2.0 | MIT License | — | 可用 |
| python | toolz | 1.2.0 | BSD-3-Clause | — | 可用 |
| python | typing-inspection | 0.4.4 | MIT | — | 可用 |
| python | typing_extensions | 4.16.0 | PSF-2.0 | — | 可用 |
| python | tzdata | 2026.6 | Apache-2.0 | — | 可用 |
| python | urllib3 | 2.8.0 | MIT | — | 可用 |
| python | uvicorn | 0.54.0 | BSD-3-Clause | — | 可用 |
| python | vulture | 2.16 | MIT License | — | 可用 |
| python | websockets | 15.0.1 | BSD License | — | 可用 |
| npm | @axe-core/playwright | 4.10.2 | MPL-2.0 | 是 | 可用 |
| npm | @babel/code-frame | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/compat-data | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/core | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/generator | 7.29.12 | MIT | 是 | 可用 |
| npm | @babel/helper-compilation-targets | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/helper-globals | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/helper-module-imports | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/helper-module-transforms | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/helper-plugin-utils | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/helper-string-parser | 7.29.11 | MIT | 是 | 可用 |
| npm | @babel/helper-validator-identifier | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/helper-validator-option | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/helpers | 7.29.10 | MIT | 是 | 可用 |
| npm | @babel/parser | 7.29.12 | MIT | 是 | 可用 |
| npm | @babel/plugin-transform-react-jsx-self | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/plugin-transform-react-jsx-source | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/template | 7.29.7 | MIT | 是 | 可用 |
| npm | @babel/traverse | 7.29.12 | MIT | 是 | 可用 |
| npm | @babel/types | 7.29.11 | MIT | 是 | 可用 |
| npm | @emnapi/core | 1.11.2 | MIT | 是 | 可用 |
| npm | @emnapi/runtime | 1.11.2 | MIT | 是 | 可用 |
| npm | @emnapi/wasi-threads | 1.2.2 | MIT | 是 | 可用 |
| npm | @esbuild/aix-ppc64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/android-arm | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/android-arm64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/android-x64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/darwin-arm64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/darwin-x64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/freebsd-arm64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/freebsd-x64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-arm | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-arm64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-ia32 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-loong64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-mips64el | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-ppc64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-riscv64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-s390x | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/linux-x64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/netbsd-arm64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/netbsd-x64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/openbsd-arm64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/openbsd-x64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/openharmony-arm64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/sunos-x64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/win32-arm64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/win32-ia32 | 0.25.12 | MIT | 是 | 可用 |
| npm | @esbuild/win32-x64 | 0.25.12 | MIT | 是 | 可用 |
| npm | @jridgewell/gen-mapping | 0.3.13 | MIT | 是 | 可用 |
| npm | @jridgewell/remapping | 2.3.5 | MIT | 是 | 可用 |
| npm | @jridgewell/resolve-uri | 3.1.2 | MIT | 是 | 可用 |
| npm | @jridgewell/sourcemap-codec | 1.6.0 | MIT | 是 | 可用 |
| npm | @jridgewell/trace-mapping | 0.3.31 | MIT | 是 | 可用 |
| npm | @napi-rs/lzma-linux-x64-gnu | 1.5.1 | MIT | 是 | 可用 |
| npm | @napi-rs/wasm-runtime | 1.2.7 | MIT | 是 | 可用 |
| npm | @nodelib/fs.scandir | 2.1.5 | MIT | 是 | 可用 |
| npm | @nodelib/fs.stat | 2.0.5 | MIT | 是 | 可用 |
| npm | @nodelib/fs.walk | 1.2.8 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-android-arm-eabi | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-android-arm64 | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-darwin-arm64 | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-darwin-x64 | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-freebsd-x64 | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-arm-gnueabihf | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-arm-musleabihf | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-arm64-gnu | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-arm64-musl | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-ppc64-gnu | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-riscv64-gnu | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-riscv64-musl | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-s390x-gnu | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-x64-gnu | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-linux-x64-musl | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-openharmony-arm64 | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-wasm32-wasi | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-win32-arm64-msvc | 11.24.2 | MIT | 是 | 可用 |
| npm | @oxc-resolver/binding-win32-x64-msvc | 11.24.2 | MIT | 是 | 可用 |
| npm | @playwright/test | 1.55.1 | Apache-2.0 | 是 | 可用 |
| npm | @rolldown/pluginutils | 1.0.0-beta.9 | MIT | 是 | 可用 |
| npm | @rollup/rollup-android-arm-eabi | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-android-arm64 | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-android-x64 | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-darwin-arm64 | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-darwin-x64 | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-freebsd-arm64 | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-freebsd-x64 | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-arm-gnueabihf | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-arm-musleabihf | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-arm64-gnu | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-arm64-musl | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-loong64-gnu | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-loong64-musl | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-ppc64-gnu | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-ppc64-musl | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-riscv64-gnu | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-riscv64-musl | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-s390x-gnu | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-x64-gnu | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-linux-x64-musl | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-openbsd-x64 | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-openharmony-arm64 | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-win32-arm64-msvc | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-win32-ia32-msvc | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-win32-x64-gnu | 4.67.4 | MIT | 是 | 可用 |
| npm | @rollup/rollup-win32-x64-msvc | 4.67.4 | MIT | 是 | 可用 |
| npm | @tybys/wasm-util | 0.10.4 | MIT | 是 | 可用 |
| npm | @types/babel__core | 7.20.5 | MIT | 是 | 可用 |
| npm | @types/babel__generator | 7.27.0 | MIT | 是 | 可用 |
| npm | @types/babel__template | 7.4.4 | MIT | 是 | 可用 |
| npm | @types/babel__traverse | 7.28.0 | MIT | 是 | 可用 |
| npm | @types/d3-geo | 3.1.0 | MIT | 是 | 可用 |
| npm | @types/estree | 1.0.10 | MIT | 是 | 可用 |
| npm | @types/geojson | 7946.0.16 | MIT | 是 | 可用 |
| npm | @types/node | 22.15.30 | MIT | 是 | 可用 |
| npm | @types/react | 19.1.6 | MIT | 是 | 可用 |
| npm | @types/react-dom | 19.1.5 | MIT | 是 | 可用 |
| npm | @types/topojson-client | 3.1.5 | MIT | 是 | 可用 |
| npm | @types/topojson-specification | 1.0.5 | MIT | 是 | 可用 |
| npm | @vitejs/plugin-react | 4.5.0 | MIT | 是 | 可用 |
| npm | axe-core | 4.10.3 | MPL-2.0 | 是 | 可用 |
| npm | baseline-browser-mapping | 2.11.35 | Apache-2.0 | 是 | 可用 |
| npm | braces | 3.0.3 | MIT | 是 | 可用 |
| npm | browserslist | 4.29.5 | MIT | 是 | 可用 |
| npm | caniuse-lite | 1.0.30001820 | CC-BY-4.0 | 是 | 可用 |
| npm | commander | 2.20.3 | MIT | 否 | 可用 |
| npm | convert-source-map | 2.0.0 | MIT | 是 | 可用 |
| npm | csstype | 3.2.3 | MIT | 是 | 可用 |
| npm | d3-array | 3.2.5 | ISC | 否 | 可用 |
| npm | d3-geo | 3.1.1 | ISC | 否 | 可用 |
| npm | debug | 4.4.3 | MIT | 是 | 可用 |
| npm | electron-to-chromium | 1.5.475 | ISC | 是 | 可用 |
| npm | esbuild | 0.25.12 | MIT | 是 | 可用 |
| npm | escalade | 3.2.0 | MIT | 是 | 可用 |
| npm | fast-glob | 3.3.3 | MIT | 是 | 可用 |
| npm | fastq | 1.20.3 | ISC | 是 | 可用 |
| npm | fd-package-json | 2.0.0 | MIT | 是 | 可用 |
| npm | fdir | 6.5.0 | MIT | 是 | 可用 |
| npm | fill-range | 7.1.1 | MIT | 是 | 可用 |
| npm | formatly | 0.3.0 | MIT | 是 | 可用 |
| npm | fsevents | 2.3.2 | MIT | 是 | 可用 |
| npm | fsevents | 2.3.3 | MIT | 是 | 可用 |
| npm | gensync | 1.0.0-beta.2 | MIT | 是 | 可用 |
| npm | glob-parent | 5.1.2 | ISC | 是 | 可用 |
| npm | internmap | 2.0.3 | ISC | 否 | 可用 |
| npm | is-extglob | 2.1.1 | MIT | 是 | 可用 |
| npm | is-glob | 4.0.3 | MIT | 是 | 可用 |
| npm | is-number | 7.0.0 | MIT | 是 | 可用 |
| npm | jiti | 2.7.0 | MIT | 是 | 可用 |
| npm | js-tokens | 4.0.0 | MIT | 是 | 可用 |
| npm | jsesc | 3.1.0 | MIT | 是 | 可用 |
| npm | json5 | 2.2.3 | MIT | 是 | 可用 |
| npm | knip | 5.88.1 | ISC | 是 | 可用 |
| npm | lru-cache | 5.1.1 | ISC | 是 | 可用 |
| npm | merge2 | 1.4.1 | MIT | 是 | 可用 |
| npm | micromatch | 4.0.8 | MIT | 是 | 可用 |
| npm | minimist | 1.2.8 | MIT | 是 | 可用 |
| npm | ms | 2.1.3 | MIT | 是 | 可用 |
| npm | nanoid | 3.3.20 | MIT | 是 | 可用 |
| npm | node-releases | 2.0.63 | MIT | 是 | 可用 |
| npm | oxc-resolver | 11.24.2 | MIT | 是 | 可用 |
| npm | picocolors | 1.1.1 | ISC | 是 | 可用 |
| npm | picomatch | 2.3.2 | MIT | 是 | 可用 |
| npm | picomatch | 4.0.7 | MIT | 是 | 可用 |
| npm | playwright | 1.55.1 | Apache-2.0 | 是 | 可用 |
| npm | playwright-core | 1.55.1 | Apache-2.0 | 是 | 可用 |
| npm | postcss | 8.5.32 | MIT | 是 | 可用 |
| npm | queue-microtask | 1.2.3 | MIT | 是 | 可用 |
| npm | react | 19.1.0 | MIT | 否 | 可用 |
| npm | react-dom | 19.1.0 | MIT | 否 | 可用 |
| npm | react-refresh | 0.17.0 | MIT | 是 | 可用 |
| npm | reusify | 1.1.0 | MIT | 是 | 可用 |
| npm | rollup | 4.67.4 | MIT | 是 | 可用 |
| npm | run-parallel | 1.2.0 | MIT | 是 | 可用 |
| npm | scheduler | 0.26.0 | MIT | 否 | 可用 |
| npm | semver | 6.3.2 | ISC | 是 | 可用 |
| npm | smol-toml | 1.9.1 | BSD-3-Clause | 是 | 可用 |
| npm | source-map-js | 1.2.2 | BSD-3-Clause | 是 | 可用 |
| npm | strip-json-comments | 5.0.3 | MIT | 是 | 可用 |
| npm | tinyglobby | 0.2.17 | MIT | 是 | 可用 |
| npm | to-regex-range | 5.0.1 | MIT | 是 | 可用 |
| npm | topojson-client | 3.1.0 | ISC | 否 | 可用 |
| npm | tslib | 2.9.1 | 0BSD | 是 | 可用 |
| npm | typescript | 5.8.3 | Apache-2.0 | 是 | 可用 |
| npm | unbash | 2.2.0 | ISC | 是 | 可用 |
| npm | undici-types | 6.21.0 | MIT | 是 | 可用 |
| npm | update-browserslist-db | 1.3.4 | MIT | 是 | 可用 |
| npm | vite | 6.4.5 | MIT | 是 | 可用 |
| npm | walk-up-path | 4.0.0 | ISC | 是 | 可用 |
| npm | world-atlas | 2.0.2 | ISC | 否 | 可用 |
| npm | yallist | 3.1.1 | ISC | 是 | 可用 |
| npm | yaml | 2.9.1 | ISC | 是 | 可用 |
| npm | zod | 4.6.7 | MIT | 是 | 可用 |
