from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INDEX = (ROOT / "static" / "index.html").read_text(encoding="utf-8")


def _runtime_source() -> str:
    start = INDEX.index("const PINYIN_RUNTIME_SRC")
    end = INDEX.index("const today =", start)
    return INDEX[start:end]


def _component_registration() -> str:
    start = INDEX.index('app.component("search-select", {')
    end = INDEX.index('app.mount("#app")', start)
    registration = INDEX[start:end].strip()
    assert registration.endswith(");")
    return registration


def _run_node(source: str, tmp_path: Path) -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the P1-36I behavior contract"
    target = tmp_path / "p1-36i-pinyin-lazy-cache.js"
    target.write_text(source, encoding="utf-8")
    result = subprocess.run(
        [node, str(target)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_pinyin_runtime_is_not_a_first_paint_dependency() -> None:
    head = INDEX.split("</head>", 1)[0]
    assert '<script src="/static/vendor/pinyin-pro-3.26.0.js"></script>' not in head
    assert '<script src="/static/vendor/vue-3.5.40.global.prod.js"></script>' in head
    assert '<script src="/static/vendor/axios-1.18.1.min.js"></script>' in head
    assert '<script src="/static/assets/time-utils.js?' in head

    runtime = _runtime_source()
    component = _component_registration()
    assert 'const PINYIN_RUNTIME_SRC = "/static/vendor/pinyin-pro-3.26.0.js";' in runtime
    assert "const PINYIN_SEARCH_CACHE_LIMIT = 4096;" in runtime
    assert 'document.createElement("script")' in runtime
    assert "if (pinyinRuntimePromise) return pinyinRuntimePromise;" in runtime
    assert "pinyinSearchTextCache.delete(" in runtime
    assert "matches.length < 50" in component
    assert "if (!includeInitials || matches.length >= 50) return matches;" in component
    assert "beforeUnmount()" in component
    assert "clearTimeout(this.timer)" in component
    assert "pinyinSearchTextCache.clear();" in INDEX
    assert "axios" not in component


def test_lazy_loading_matching_cache_and_unmount_contract(tmp_path: Path) -> None:
    helper_source = _runtime_source()
    registration = _component_registration()
    script = f"""
const helperSource = {json.dumps(helper_source, ensure_ascii=False)};
const componentRegistration = {json.dumps(registration, ensure_ascii=False)};
const expect = (value, message) => {{ if (!value) throw new Error(message); }};

let nextTimerId = 1;
const timers = new Map();
globalThis.setTimeout = callback => {{
  const id = nextTimerId++;
  timers.set(id, {{callback, cancelled:false}});
  return id;
}};
globalThis.clearTimeout = id => {{
  const timer = timers.get(id);
  if (timer) timer.cancelled = true;
}};
async function runTimers() {{
  for (const [id, timer] of [...timers.entries()]) {{
    timers.delete(id);
    if (!timer.cancelled) await timer.callback();
  }}
}}
function resetTimers() {{ timers.clear(); }}
async function flushPromises() {{
  await Promise.resolve();
  await Promise.resolve();
}}

function createEnvironment(pinyinFunction=null) {{
  const scripts = [];
  const windowObject = {{}};
  if (pinyinFunction) windowObject.pinyinPro = {{pinyin:pinyinFunction}};
  const documentObject = {{
    createElement(tag) {{
      expect(tag === "script", "lazy loader created a non-script element");
      const listeners = {{}};
      return {{
        src:"", async:false, dataset:{{}},
        addEventListener(type, callback) {{ listeners[type] = callback; }},
        fire(type) {{ listeners[type]?.(); }},
      }};
    }},
    head: {{ appendChild(script) {{ scripts.push(script); }} }},
  }};
  const factory = new Function(
    "window", "document",
    `${{helperSource}}
     let registeredComponent = null;
     const app = {{component(name, definition) {{
       if (name === "search-select") registeredComponent = definition;
     }}}};
     ${{componentRegistration}}
     return {{
       component:registeredComponent,
       ensurePinyinRuntime,
       cachedSearchText,
       runtimeState:() => pinyinRuntimeState,
       runtimePromise:() => pinyinRuntimePromise,
       cacheSize:() => pinyinSearchTextCache.size,
       cacheLimit:PINYIN_SEARCH_CACHE_LIMIT,
     }};`
  );
  const api = factory(windowObject, documentObject);
  expect(api.component, "search-select component was not registered");
  return {{api, windowObject, documentObject, scripts}};
}}

function mountComponent(environment, options, query="") {{
  const definition = environment.api.component;
  const emitted = [];
  const vm = {{
    ...definition.data(), options, modelValue:"", labelKey:"name", valueKey:"id",
    placeholder:"", allowEmpty:false, disabled:false,
    $emit(...args) {{ emitted.push(args); }},
  }};
  for (const [name, method] of Object.entries(definition.methods)) vm[name] = method.bind(vm);
  for (const [name, getter] of Object.entries(definition.computed)) {{
    Object.defineProperty(vm, name, {{get:getter.bind(vm), configurable:true}});
  }}
  definition.mounted.call(vm);
  vm.query = query;
  return {{vm, emitted, definition}};
}}

function initialsPinyin(counter) {{
  const letters = {{"天":"t", "明":"m", "华":"h", "苏":"s", "州":"z"}};
  return text => {{
    counter.count += 1;
    return [...String(text)].map(char => letters[char] || char);
  }};
}}

(async () => {{
  // Chinese, numbers and stored codes remain synchronous and do not load pinyin.
  resetTimers();
  const success = createEnvironment();
  const options = [
    {{id:1, name:"天明包装", customer_code:"C001"}},
    {{id:2, name:"测试纸箱", product_code:"22000008"}},
    {{id:3, name:"A1标准箱", product_code:"A1"}},
    {{id:4, name:"天华物料", product_code:"TH-001"}},
  ];
  const first = mountComponent(success, options, "天明");
  first.vm.onInput();
  expect(first.vm.filtered.map(row => row.id).join(",") === "1", "Chinese direct match failed before runtime load");
  expect(success.scripts.length === 0, "Chinese input loaded the pinyin runtime");
  first.vm.query = "22000008";
  first.vm.onInput();
  expect(first.vm.filtered.map(row => row.id).join(",") === "2", "numeric direct match failed before runtime load");
  expect(success.scripts.length === 0, "numeric input loaded the pinyin runtime");
  first.vm.query = "a1";
  first.vm.onInput();
  expect(first.vm.filtered.map(row => row.id).join(",") === "3", "A1 direct code did not match before runtime load");
  expect(success.scripts.length === 0, "A1 direct code loaded the pinyin runtime");
  first.vm.query = "th-001";
  first.vm.onInput();
  expect(first.vm.filtered.map(row => row.id).join(",") === "4", "TH-001 direct code did not match before runtime load");
  expect(success.scripts.length === 0, "TH-001 direct code loaded the pinyin runtime");

  // Two components requesting initials share one local script and one Promise.
  first.vm.query = "tm";
  first.vm.onInput();
  expect(success.scripts.length === 1, "first ASCII input did not create exactly one script");
  expect(success.scripts[0].src === "/static/vendor/pinyin-pro-3.26.0.js", "runtime source is not the pinned local vendor");
  expect(success.scripts[0].async === true, "runtime script is not asynchronous");
  const sharedPromise = success.api.runtimePromise();
  const second = mountComponent(success, options, "tm");
  second.vm.onInput();
  expect(success.scripts.length === 1, "concurrent components created duplicate runtime scripts");
  expect(success.api.runtimePromise() === sharedPromise, "concurrent components did not share one runtime Promise");
  const successCalls = {{count:0}};
  success.windowObject.pinyinPro = {{pinyin:initialsPinyin(successCalls)}};
  success.scripts[0].fire("load");
  await flushPromises();
  expect(success.api.runtimeState() === "ready", "runtime did not become ready after a valid load");
  expect(first.vm.pinyinRuntimeVersion === 1 && second.vm.pinyinRuntimeVersion === 1, "mounted components did not refresh after runtime load");
  expect(first.vm.filtered.some(row => row.id === 1), "天明 did not match tm after runtime load");

  // A failed runtime never clears direct results and never retries on every key/component.
  resetTimers();
  const failed = createEnvironment();
  const direct = mountComponent(failed, [{{id:3, name:"天华", customer_code:"TH"}}], "th");
  direct.vm.onInput();
  expect(direct.vm.filtered.length === 1, "direct code was hidden while runtime was loading");
  expect(failed.scripts.length === 1, "failed-runtime scenario did not start one request");
  failed.scripts[0].fire("error");
  await flushPromises();
  expect(failed.api.runtimeState() === "failed", "runtime error did not enter the failed state");
  expect(direct.vm.filtered.length === 1, "runtime failure cleared a direct match");
  const afterFailure = mountComponent(failed, [{{id:4, name:"苏州客户"}}], "sz");
  afterFailure.vm.onInput();
  await flushPromises();
  expect(failed.scripts.length === 1, "runtime failure caused a retry storm");

  // Direct ASCII matches must not pay for pinyin conversion.
  resetTimers();
  const directCalls = {{count:0}};
  const readyDirect = createEnvironment(initialsPinyin(directCalls));
  const directMatch = mountComponent(
    readyDirect,
    [{{id:5, name:"天明", customer_code:"TM01"}}],
    "tm01",
  );
  expect(directMatch.vm.filtered.length === 1, "ready-runtime direct code did not match");
  expect(directCalls.count === 0, "direct match still performed pinyin conversion");

  // Identical primitive text is cached across repeated input and replacement objects.
  const cacheCalls = {{count:0}};
  const cached = createEnvironment(initialsPinyin(cacheCalls));
  const chineseOnly = mountComponent(cached, [{{id:6, name:"天明"}}], "tm");
  expect(chineseOnly.vm.filtered.length === 1, "initials match failed with ready runtime");
  expect(cacheCalls.count === 1, "first initials match did not convert exactly once");
  expect(chineseOnly.vm.filtered.length === 1, "repeated initials match changed its result");
  const replacement = mountComponent(cached, [{{id:6, name:"天明"}}], "tm");
  expect(replacement.vm.filtered.length === 1, "replacement object with identical text did not match");
  expect(cacheCalls.count === 1, "identical text from a new object was converted again");
  replacement.vm.options = [{{id:6, name:"天华"}}];
  replacement.vm.query = "th";
  expect(replacement.vm.filtered.length === 1, "changed candidate field did not recalculate");
  expect(cacheCalls.count === 2, "changed candidate text reused a stale pinyin cache entry");
  for (let index = 0; index < cached.api.cacheLimit + 20; index += 1) {{
    cached.api.cachedSearchText(`bounded-${{index}}`, false);
  }}
  expect(cached.api.cacheSize() <= cached.api.cacheLimit, "search text cache exceeded its fixed upper bound");

  // The first 50 direct matches stop iteration before touching later candidates.
  const earlyCalls = {{count:0}};
  const early = createEnvironment(initialsPinyin(earlyCalls));
  const firstFifty = Array.from({{length:50}}, (_, index) => ({{
    id:index + 1, name:`直接候选${{index + 1}}`, customer_code:`MATCH-${{index + 1}}`,
  }}));
  const poison = {{id:999, customer_code:"MATCH-999"}};
  Object.defineProperty(poison, "name", {{
    enumerable:true,
    get() {{ throw new Error("filtered scanned beyond its first 50 matches"); }},
  }});
  const earlyStop = mountComponent(early, [...firstFifty, poison], "match");
  expect(earlyStop.vm.filtered.length === 50, "filtered did not return exactly the first 50 matches");
  expect(earlyCalls.count === 0, "direct first-50 results performed pinyin conversion");

  // A later direct code must not be displaced by 50 earlier initials-only matches.
  const priorityCalls = {{count:0}};
  const priority = createEnvironment(initialsPinyin(priorityCalls));
  const initialsOnly = Array.from({{length:55}}, (_, index) => ({{
    id:index + 1, name:`天明候选${{index + 1}}`, customer_code:`C-${{index + 1}}`,
  }}));
  const lateDirect = {{id:999, name:"后置直接编码", customer_code:"TM"}};
  const directPriority = mountComponent(priority, [...initialsOnly, lateDirect], "tm");
  expect(directPriority.vm.filtered.length === 50, "direct-priority search did not keep the 50-result limit");
  expect(directPriority.vm.filtered[0].id === 999, "later direct code was displaced by earlier pinyin matches");
  expect(directPriority.vm.filtered.some(row => row.id === 999), "later direct code is missing from results");

  // Unmount cancels the 220ms event and ignores a late runtime completion.
  resetTimers();
  const late = createEnvironment();
  const unmounted = mountComponent(late, [{{id:7, name:"天明"}}], "tm");
  unmounted.vm.onInput();
  expect(late.scripts.length === 1, "late-load scenario did not start the runtime request");
  unmounted.definition.beforeUnmount.call(unmounted.vm);
  late.windowObject.pinyinPro = {{pinyin:initialsPinyin({{count:0}})}};
  late.scripts[0].fire("load");
  await flushPromises();
  await runTimers();
  expect(unmounted.vm.pinyinRuntimeVersion === 0, "unmounted component reacted to a late runtime load");
  expect(unmounted.emitted.length === 0, "unmounted component emitted a stale search or selection event");
}})().catch(error => {{ console.error(error); process.exit(1); }});
"""
    _run_node(script, tmp_path)
