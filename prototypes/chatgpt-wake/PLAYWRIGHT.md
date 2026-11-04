# Isolated extension browser tests

Marco requested Playwright tests on 4 November. This is one test-only slice on
7293ed1025; production behavior, installed bytes, and the stopped live backend stay
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
provider acceptance. Real navigation tests remain NOT_RUN. The known missing
drained-HALTED reconciliation remains pending; tests do not reset it to force a
pass. Failures must be recorded without changing production code to make tests
green. Traces are retained on test failure in the isolated test output directory.

Current execution checkpoint: dependency installation was rejected before process
execution by automatic approval (`approval required by policy, but AskForApproval
is set to Never`); browser installation and suite execution are NOT_RUN. Syntax
checks pass. Feedback: 30971582-fec6-5549-9abf-bffc137351d1. No alternative installer
or directory is used to bypass that rejection.
