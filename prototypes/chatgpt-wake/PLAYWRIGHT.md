# Isolated extension browser tests

Marco requested Playwright tests on 4 November. This is one test-only slice on
the reviewed Pair candidate; production behavior, installed bytes, and the live backend stay
unchanged. No PR publication or landing is included. Existing state tests retain
STOP/restart/identity coverage; this suite adds the browser boundary rather than
porting that state-machine fixture. Expected surface: one small spec/fixture,
pinned development dependency/config, and this guide. Material fixture growth or
any product fix requires a separate disposition.

From this directory, setup and execution are:

```sh
npm install --ignore-scripts
npx playwright install chromium
npm run test:playwright
```

The [official extension setup](https://playwright.dev/docs/chrome-extensions) uses
bundled Chromium with a persistent context and `channel: chromium` for headless
MV3 support. Each case uses a disposable profile, actual extension files, public
Verify/Bind/Check messages through its popup page, and an ephemeral loopback
metadata stub. The temporary worker copy changes exactly one backend URL literal;
the original source hash is attached and checked unchanged. No live backend,
credentials, existing browser profiles, or ChatGPT network requests are used.
Every ChatGPT URL is fulfilled locally; other page requests are blocked except
the test's exact backend. Default alarms are disabled in the isolated profile so
each public Check has a deterministic observation window.

Cases cover the three navigation transitions, duplicate matching tabs, same IDs
versus new deliveries, preserving drafts/thinking state, delayed model/render and
Send enabling, one owned no-click retry, and no duplicate after a click retaining
text. A persistent error remains fenced after the retry. The oracle is a fixture
turn plus insertion count, not the extension's `click-attempted` result alone.

The small editor model updates after render frames and a configurable delay. It
does **not** implement ChatGPT's rich-editor model, busy/timeout recovery, or prove
provider acceptance. Real navigation tests remain NOT_RUN. The tenth case exercises explicit drained-HALTED Pair reconciliation without
resetting storage to force a pass. Failures must be recorded without changing production code to make tests
green. Traces are retained on test failure in the isolated test output directory.

Current qualification: all ten Playwright cases passed using existing Playwright
1.64.1 packages and Brave 703 in disposable profiles (15.8 seconds). The source
worker remains unchanged; this is local fixture qualification, not real ChatGPT
acceptance or the three human navigation proofs. Marco reported an extension
Reload; the loaded worker hash is not independently attested.

For an already installed compatible Chromium executable, the optional test-only
`WAKE_TEST_BROWSER` overrides the default bundled Chromium launch. From this
folder, the repeatable command on this host is:

```sh
WAKE_TEST_BROWSER=/snap/brave/703/opt/brave.com/brave/brave ./node_modules/.bin/playwright test --config=playwright.config.mjs
```

The Pair writer can use the existing dependency tree without installing anything:

```sh
ln -s /home/marco/.local/state/switchstand/worktrees/chatgpt-wake-playwright-20261104/prototypes/chatgpt-wake/node_modules node_modules
```

Only create that link if `node_modules` is absent. Dependency installation remains
on HOLD; do not run the setup commands above until Marco resumes installation.
The earlier host npm action was interrupted despite its UI rejection; the existing
CLI and packages were usable, but full installation completion and Chromium
download are not claimed. Local test execution was explicitly resumed by Marco.

The [executablePath API](https://playwright.dev/docs/api/class-browsertype#browser-type-launch-persistent-context-option-executable-path)
allows an existing browser; compatibility with arbitrary versions is not guaranteed.
No existing browser profile, live ChatGPT request, credential, or backend is used.
The separate Node and CDP/MV3 suites also passed Pair safety checks.
