/* Isolated Chrome contract/interaction checks. Every API is intercepted; no ERP connection. */
const { chromium } = require("playwright");
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const repo = pathResolveRepo();
function pathResolveRepo() { return require("node:path").resolve(__dirname, "../.."); }
const path = require("node:path");
const base = process.env.MOBILE_UI_URL || "http://127.0.0.1:18748";
const siteOrigin = new URL(base).origin;
const basePath = new URL(base).pathname.replace(/\/$/, "");
const out =
  process.env.MOBILE_UI_EVIDENCE ||
  "/Users/kimiyf/ERP-Migration/evidence/issue48-mobile-ui-20261020";
const p = {
  id: 7,
  product_id: 7,
  product_code: "Z.001.000227",
  product_name: "加厚48入装风机纸箱",
  customer_name: "测试客户甲",
  specification: "1150×795×906mm",
  is_active: true,
  report_length_mm: 1230,
  report_width_mm: 880,
  inventory_summary: {
    finished: { actual: 232, available: 232, reserved: 0, unit: "只" },
  },
  inventory_positions: [
    { inventory_type: "finished", location_label: "三楼 EDIT-028·左侧第1位" },
  ],
  drawings: {
    items: [
      {
        id: "printing",
        kind: "image",
        preview_url: "/api/mobile/erp/products/7/drawings/printing/preview",
        original_url: "/api/mobile/erp/products/7/drawings/printing/original",
      },
    ],
  },
};
const positions = [
  {
    lot_id: 70,
    location_id: 101,
    location_label: "三楼 EDIT-028·左侧第1位",
    actual: 209,
    available: 209,
    unit: "只",
    inventory_type: "finished",
  },
  {
    lot_id: 71,
    location_id: 102,
    location_label: "三楼 EDIT-028·左侧第6位",
    actual: 23,
    available: 23,
    unit: "只",
    inventory_type: "finished",
  },
];
const detail = {
  product: p,
  production: {
    process_steps: [{ label: "印刷" }, { label: "模切" }, { label: "粘箱" }],
    molds: [{ location_short_label: "三楼 M架12" }],
    joining_method: "粘箱",
    crease_values_mm: [335, 160, 335],
    report_length_mm: 1230,
    report_width_mm: 880,
  },
  inventory: {
    items: positions,
    summary: p.inventory_summary,
    source_summary: {
      stocktake: { label: "盘点", actual: 23 },
      replenishment: { label: "补库生产", actual: 209 },
      order: { label: "订单生产", actual: 0 },
    },
  },
};
const good = {
  lot_id: 70,
  product_id: 7,
  product_code: p.product_code,
  product_name: p.product_name,
  customer_short_name: "测试客户甲",
  quantity_total: 209,
  quantity_available: 209,
  unit: "只",
};
const incoming = {
  item_id: "sr17",
  product_id: 7,
  product_code: p.product_code,
  product_name: p.product_name,
  customer_name: p.customer_name,
  supplier_name: "测试供应商",
  supplier_delivery_time: "2026-10-20 09:40",
  requisition_spec: "1230×880",
  flute_type: "B楞",
  quantity: 500,
  remaining_quantity: 300,
};
const drawing =
  '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="240"><rect x="20" y="30" width="360" height="180" fill="#fff" stroke="#2e7d4f" stroke-width="3"/><path d="M90 30v180M310 30v180M20 90h360M20 150h360" fill="none" stroke="#2e7d4f" stroke-dasharray="5"/></svg>';
let activeBrowser;
async function run() {
  await fs.mkdir(out, { recursive: true });
  const browser = await chromium.launch({
    executablePath:
      "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    headless: true,
  });
  activeBrowser = browser;
  const context = await browser.newContext({
    viewport: { width: 375, height: 812 },
    isMobile: true,
    hasTouch: true,
    deviceScaleFactor: 1,
    colorScheme: "light",
  });
  let sevenDayPages = false;
  let role = "admin",
    received = false,
    delaySearch = false,
    failReceipt = false,
    receiptAttempts = 0,
    unauthorized = false,
    hiddenInventory = false;
  const requests = [],
    consoleErrors = [],
    networkLeaks = [],
    results = [];
  await context.route("**/*", async (route) => {
    const url = new URL(route.request().url()),
      pathname = url.pathname;
    if (pathname.startsWith("/api/")) {
      requests.push({
        method: route.request().method(),
        path: pathname,
        query: Object.fromEntries(url.searchParams),
        body: route.request().postData(),
      });
      let data,
        status = 200;
      if (pathname === "/api/mobile/erp/dimension-settings") return route.fulfill({json:{near_mm:5,expanded_mm:20,can_edit:false,version:1}});
      if (unauthorized) {
        data = { detail: "登录已失效" };
        status = 401;
      } else if (pathname === "/api/auth/me")
        data = {
          permissions: role === "admin" ? ["warehouse.view", "incoming.view"] : ["incoming.view"],
          user: {
            id: 1,
            username: "fixture",
            real_name: "测试",
            role,
            is_active: true,
            must_change_password: false,
          },
        };
      else if (pathname === "/api/mobile/erp/shell")
        data = {
          user: { id: 1, role, display_name: "测试" },
          entries: [
            { id: "lookup" },
            {
              id: "warehouse",
              can_execute: role === "admin",
              can_correct: role === "admin",
            },
            { id: "incoming", can_execute: role === "admin" },
            { id: "pre_delivery" },
            { id: "production" },
          ],
          search_categories: ["orders", "inventory"],
          management_summary_allowed: role !== "workshop",
          delivery_margin_allowed: role === "admin" || role === "boss",
        };
      else if (pathname === "/api/mobile/erp/search")
        data = {
          groups: [
            {
              total: 4,
              page: 1,
              items: [
                {
                  order_item_id: 8,
                  product_id: 7,
                  product_code: p.product_code,
                  product_name: p.product_name,
                  customer_name: p.customer_name,
                  remaining_quantity: 50,
                  specification: p.specification,
                },
              ],
            },
          ],
        };
      else if (pathname === "/api/dashboard/overview")
        data = {
          month: "2026-10",
          business_status_counts: {
            pending_incoming: 2,
            pending_delivery: 3,
            partially_delivered: 1,
          },
        };
      else if (pathname === "/api/dashboard/customer-delivery-margin")
        data = {
          summary: {
            sales_amount: "58600",
            material_cost: "32200",
            material_margin: "26400",
            material_margin_rate: ".4505",
          },
        };
      else if (pathname === "/api/product-workbench/search") {
        if (delaySearch && url.searchParams.get("q") === "旧")
          await new Promise((r) => setTimeout(r, 500));
        data = {
          items: [
            {
              ...p,
              ...(url.searchParams.get("sort_by") === "dimension_match" ? {dimension_match:{score_percent:99.7,total_delta_mm:3}} : {}),
              product_name:
                url.searchParams.get("q") === "新"
                  ? "最新搜索结果"
                  : p.product_name,
            },
          ],
          total: 1,
          page: 1,
          page_size: 12,
        };
      } else if (pathname === "/api/product-workbench/products/7")
        data = hiddenInventory
          ? {
              ...detail,
              inventory: { visibility: "hidden_by_permission", items: [] },
            }
          : detail;
      else if (pathname.includes("/drawings/"))
        return route.fulfill({
          status: 200,
          contentType: "image/svg+xml",
          body: drawing,
        });
      else if (pathname === "/api/mobile/erp/warehouse/map/floors")
        data = {
          floors: [
            {
              floor_code: "3F",
              floor_name: "三楼",
              floor_number: 3,
              areas: [{ area_code: "EDIT-028", area_name: "EDIT-028" }],
            },
            {
              floor_code: "2F",
              floor_name: "二楼",
              floor_number: 2,
              areas: [{ area_code: "BOARD", area_name: "纸板区" }],
            },
          ],
          can_execute: role === "admin",
          can_correct: role === "admin",
        };
      else if (pathname.includes("/warehouse/map/overview/"))
        data = {
          map_status: "ready",
          areas: [
            {
              feature_id: "measured-zone",
              area_code: pathname.endsWith("3F") ? "EDIT-028" : "BOARD",
              area_name: pathname.endsWith("3F") ? "EDIT-028" : "纸板区",
              points: [
                [0, 0],
                [30000, 0],
                [30000, 40000],
                [0, 40000],
              ],
            },
          ],
        };
      else if (pathname.includes("/warehouse/map/floors/"))
        data = {
          map_status: "ready", floor_name: "三楼", area_name: "E28", bounds_mm:{min_x:0,min_y:0,max_x:30000,max_y:40000}, features:[],
          locations: positions.map((position, i) => ({
            storage_type: i ? "ground" : "rack", map_rack_id: i ? null : "rack-7", rack_display_name: "G1货架", level_no: 2, slot_no: 1, can_select_target: true,
            location_id: position.location_id,
            location_name: position.location_label,
            short_location_label: position.location_label,
            geometry: {
              left_pct: 10 + i * 40,
              top_pct: 20,
              width_pct: 28,
              height_pct: 40,
            },
            goods: i ? [] : [good],
          })),
        };
      else if (pathname.includes("locations-search"))
        data = {
          items: [
            {
              location_id: 101,
              location_name: positions[0].location_label,
              floor_code: "3F",
              area_code: "EDIT-028",
            },
          ],
          total: 1,
        };
      else if (pathname === "/api/incoming/pending")
        data = { items: received ? [] : [incoming] };
      else if (pathname === "/api/incoming/received")
        data = { items: sevenDayPages ? [{...incoming, product_code: "000205", receipt_item_id: Number(url.searchParams.get("page")) === 2 ? 522 : 502, history_key: "history-"+url.searchParams.get("page"), received_quantity_this_time: 200, material_received_at: "2026-10-15T08:00:00+08:00"}] : received ? [{ ...incoming, receipt_item_id: 501, history_key: "receipt-501", received_quantity_this_time: 300, remaining_quantity: 0 }] : [], total: sevenDayPages ? 21 : received ? 1 : 0, page: Number(url.searchParams.get("page") || 1) };
      else if (pathname === "/api/mobile/erp/incoming/sr17/production-detail")
        data = {
          incoming_item: incoming,
          production_tasks: [
            {
              process_tags: ["印刷", "模切", "粘箱"],
              drawing_path: p.drawings.items[0].preview_url,
            },
          ],
        };
      else if (pathname === "/api/incoming/receive/sr17") {
        receiptAttempts++;
        const key = JSON.parse(route.request().postData()).idempotency_key;
        assert.match(key, /^receive-/);
        const previous = requests.filter(r => r.path === pathname && r.method === "PUT");
        if (previous.length > 1) assert.equal(key, JSON.parse(previous[0].body).idempotency_key);
        await new Promise((r) => setTimeout(r, 180));
        if (failReceipt) {
          status = 409;
          data = { detail: "来料状态已变化" };
        } else {
          received = true;
          data = { ok: true };
        }
      } else if (pathname === "/api/incoming/receipt-items/501/revert") {
        const payload = JSON.parse(route.request().postData());
        assert.equal(payload.reason, "数量录错");
        assert.match(payload.idempotency_key, /^revert-/);
        received = false;
        data = { ok: true };
      } else if (pathname === "/api/auth/logout") data = { ok: true };
      else {
        status = 404;
        data = { detail: `Unhandled mock ${pathname}` };
      }
      return route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(data),
        headers: {
          "access-control-allow-origin": base,
          "access-control-allow-credentials": "true",
        },
      });
    }
    if (url.origin === siteOrigin && pathname === "/mobile/erp.html") return route.fulfill({contentType:"text/html",body:await fs.readFile(path.join(repo,"static/mobile_erp.html"),"utf8")});
    if (url.origin === siteOrigin && pathname.startsWith("/static/")) {
      const file = path.resolve(repo,"."+pathname);
      if (file.startsWith(path.join(repo,"static")+path.sep)) {
        try { return route.fulfill({path:file}); } catch (_) { return route.abort(); }
      }
    }
    if (url.origin !== siteOrigin) {
      networkLeaks.push(route.request().url());
      return route.abort();
    }
    return route.continue();
  });
  const page = await context.newPage();
  page.on("pageerror", (e) => consoleErrors.push(e.message));
  async function clean(name, screenshot = true) {
    assert.equal(
      await page
        .locator("body")
        .evaluate((el) => el.scrollWidth <= window.innerWidth),
      true,
      name + ": horizontal overflow",
    );
    assert.doesNotMatch(
      await page.locator(".mobile-app").innerText(),
      /\b[A-Z]\.\d+\.\d+\b|EDIT-\d+/i,
      name + ": long ERP code",
    );
    if (screenshot)
      await page.screenshot({
        path: path.join(out, `${name}.png`),
        fullPage: true,
      });
    results.push(name);
  }
  await page.goto(base + "/mobile");
  await page.getByText("58,600", { exact: true }).waitFor();
  assert.equal(await page.locator(".m-bigbtn").count(), 4);
  assert.equal(
    await page
      .locator(".m-bigbtn")
      .first()
      .evaluate((el) => el.getBoundingClientRect().height >= 48),
    true,
  );
  await clean("01-home");
  await page.getByRole('link', { name: /未送齐订单/ }).click();
  await page.getByRole('heading', { name: '未送齐订单' }).waitFor();
  await page.locator('.m-product-row').waitFor();
  assert.ok(requests.some(r=>r.path==='/api/mobile/erp/search'&&r.query.unfulfilled_only==='true'));
  results.push('unfulfilled-order-todo');
  await page.goto(base+'/mobile');
  await page.locator('.m-bigbtn').first().waitFor();

  await page.getByRole("link", { name: "现场查询", exact: true }).click();
  await page.getByLabel("成品长").fill("420");
  await page.getByLabel("成品宽").fill("310");
  await page.getByLabel("成品高").fill("260");
  await page.getByRole("button",{name:"找产品",exact:true}).click();
  await page.getByText("99.7%",{exact:true}).waitFor();
  assert.ok(requests.some(r=>r.query.dimension_basis==="finished"&&r.query.sort_by==="dimension_match"&&r.query.height==="260"&&r.query.tolerance_mm==="20"));
  assert.match(await page.locator(".m-product-mid b").innerText(),/测试客户甲/);
  await clean("02-finished-dimension-rank");
  await page.getByLabel("查询内容").fill("227");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await page.locator(".m-product-row").waitFor();
  await clean("02-query");
  await page.locator(".m-product-row").click();
  await page.getByRole("heading", { name: p.product_name }).waitFor();
  const actionBox = await page
    .getByRole("button", { name: "查看图纸" })
    .boundingBox();
  const navBox = await page.locator(".m-tabbar").boundingBox();
  await page.screenshot({ path: path.join(out, "03-product-fit.png") });
  await fs.writeFile(
    path.join(out, "product-fit.json"),
    JSON.stringify({
      actionBox,
      navBox,
      scroll: await page
        .locator(".m-main")
        .evaluate((e) => ({
          height: e.clientHeight,
          scrollHeight: e.scrollHeight,
        })),
    }),
  );
  assert.ok(
    actionBox.y + actionBox.height <= navBox.y,
    "product action must not be covered by navigation",
  );
  await clean("03-product");
  await page.getByRole("button", { name: "查看图纸" }).click();
  await page.getByRole("dialog").waitFor();
  assert.match(await page.getByRole("dialog").innerText(), /图-227/);
  await page.keyboard.press("Escape");
  assert.equal(await page.getByRole("dialog").count(), 0);
  await page.getByRole("button", { name: "盘点", exact: true }).click();
  assert.match(
    await page.getByRole("link", { name: /E28 左1/ }).getAttribute("href"),
    /location_id=101/,
  );
  await page.keyboard.press("Escape");
  await page.getByRole("link", { name: "查询", exact: true }).click();
  await page.getByRole("tab", { name: "认纸板", exact: true }).click();
  await page.getByLabel("纸板长").fill("1230");
  await page.getByLabel("纸板宽").fill("880");
  await page.getByRole("button", { name: "认纸板", exact: true }).click();
  await page.locator(".m-product-row").waitFor();
  assert.ok(
    requests.some(
      (r) =>
        r.query.dimension_basis === "report" &&
        r.query.tolerance_mm === "5" &&
        r.query.length === "1230",
    ),
  );
  await clean("04-recognize");
  await page.getByLabel("尺寸容差").fill("10");
  await page.getByLabel("尺寸容差").blur();
  await page.waitForFunction(() => location.search.includes("tolerance=10"));
  await page.getByRole("link", { name: "仓库", exact: true }).click();
  await page.getByRole("link", { name: "E28", exact: true }).waitFor();
  const mapBox = await page.locator(".m-mapview").boundingBox();
  const mainBox = await page.locator(".m-main").boundingBox();
  assert.ok(mapBox.height > mainBox.height * .75, "floor plan must use available screen height");
  await clean("05-warehouse-fullscreen");
  await page.getByRole("button", { name: "放大地图" }).click();
  await clean("05-warehouse-zoom",false);
  await page.getByRole("button", { name: "全图",exact:true }).click();
  await page.getByRole("link", { name: "E28", exact: true }).click();
  await page.locator(".warehouse-rack-elevation").waitFor();
  assert.match(page.url(),/mobile_page=warehouse.*return_mobile_map=1/);
  assert.equal(await page.locator('iframe[src*="mobile/erp"]').count(),0);
  await page.locator(".warehouse-rack-elevation summary").click();
  await page.locator(".warehouse-rack-elevation [data-location-id='101']").waitFor({state:"visible"});
  assert.equal(await page.locator("#warehouseMapStage [data-location-id='102']").count(),1);
  assert.equal(await page.locator("#warehouseRackList [data-location-id='102']").count(),0);
  await page.screenshot({path:path.join(out,"05-native-area-rack-and-pallet.png"),fullPage:true});
  await page.getByRole("button",{name:"返回全图",exact:true}).click();
  await page.locator(".m-map-area").waitFor();
  assert.equal(new URL(page.url()).searchParams.get("floor"),"3F");
  results.push("native-rack-elevation-pallet-topdown-return");
  await page.getByRole("button", { name: "二楼", exact: true }).click();
  await page.getByRole("link", { name: "纸板区", exact: true }).waitFor();
  await page.goto(base + "/mobile-receive");
  await page.locator(".m-receive-card").waitFor();
  assert.equal(new URL(page.url()).pathname, basePath + "/mobile/receive");
  assert.doesNotMatch(
    await page.locator(".m-receive-head").innerText(),
    /测试供应商|测试客户甲|227/,
  );
  await page.locator(".m-receive-head").click();
  await page.getByRole("button", { name: "确认收料", exact: true }).waitFor();
  await page.getByText("印刷 → 模切 → 粘箱", { exact: true }).waitFor();
  await clean("06-receive");
  failReceipt = true;
  await page.getByRole("button", { name: "确认收料", exact: true }).click();
  await page.getByText("来料状态已变化", { exact: false }).waitFor();
  assert.equal(await page.locator(".m-receive-card").count(), 1);
  failReceipt = false;
  await page.getByRole("button", { name: "确认收料", exact: true }).dblclick();
  await page.getByText("已确认收料", { exact: true }).waitFor();
  assert.equal(receiptAttempts, 2);
  await page.getByRole("button", { name: "已收料", exact: true }).click();
  assert.match(await page.locator(".m-receive-head").innerText(), /300/);
  await page.locator(".m-receive-head").click();
  await page.getByRole("button", { name: "撤销收料", exact: true }).click();
  await page.getByLabel("撤销原因").fill("数量录错");
  await page.getByRole("button", { name: "确认撤销" }).click();
  await page.getByText("已撤销收料", { exact: true }).waitFor();
  results.push("receipt-failure-retry-double-submit-and-revert");
  sevenDayPages = true;
  await page.getByRole("button",{name:"已收料",exact:true}).click();
  await page.getByRole("button",{name:"刷新收料"}).click();
  await page.getByText("近7天 · 共 21 条",{exact:true}).waitFor();
  await page.getByRole("button",{name:"下一页",exact:true}).click();
  await page.getByText("2 / 2",{exact:true}).waitFor();
  assert.ok(requests.some(r=>r.path==="/api/incoming/received"&&r.query.days==="7"&&r.query.page==="2"));
  await clean("06-seven-day-received-pagination");
  sevenDayPages = false;
  // Stale responses must not replace a newer search or another tab's state.
  await page.goto(base + "/mobile/query");
  delaySearch = true;
  await page.getByLabel("查询内容").fill("旧");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await page.getByRole("tab", { name: "查货位", exact: true }).click();
  await page.getByLabel("查询内容").fill("E28");
  await page.getByRole("button", { name: "查询", exact: true }).click();
  await page.locator(".m-location-result").waitFor();
  await page.waitForTimeout(600);
  assert.equal(await page.locator(".m-product-row").count(), 0);
  results.push("stale-search-response-rejected");
  // Camera fallback and arbitrary QR URLs do not navigate to foreign hosts.
  await page.getByRole("button", { name: "扫码", exact: true }).click();
  await page.getByRole("dialog").waitFor();
  await page
    .getByLabel("手工输入条码")
    .fill("https://evil.example/mobile/steal");
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "查询", exact: true })
    .click();
  await page.getByText("条码地址无效", { exact: true }).waitFor();
  results.push("scanner-fallback-and-url-origin-gate");
  // Role changes are new isolated app instances. Workers must never fetch cost endpoints.
  role = "workshop";
  const costBefore = requests.filter((r) =>
    r.path.includes("customer-delivery-margin"),
  ).length;
  await page.goto(base + "/mobile");
  await page.locator(".m-bigbtn").first().waitFor();
  await page.waitForTimeout(100);
  assert.equal(await page.locator(".m-summary").count(), 0);
  assert.equal(
    requests.filter((r) => r.path.includes("customer-delivery-margin")).length,
    costBefore,
  );
  await page.goto(base + "/mobile/receive");
  await page.locator(".m-receive-head").click();
  assert.equal(
    await page.getByRole("button", { name: "确认收料", exact: true }).count(),
    0,
  );
  results.push("employee-no-finance-and-readonly-actions");
  role = "finance";
  await page.goto(base + "/mobile");
  await page.locator(".m-summary").waitFor();
  assert.equal(
    await page.locator(".m-summary-grid b").first().innerText(),
    "—",
  );
  assert.equal(
    requests.filter((r) => r.path.includes("customer-delivery-margin")).length,
    costBefore,
  );
  results.push("finance-existing-entitlement-kept");
  role = "admin";
  hiddenInventory = true;
  await page.goto(base + "/mobile/products/7");
  await page.getByRole("heading", { name: p.product_name }).waitFor();
  assert.equal(await page.locator(".m-stock-number").innerText(), "—");
  hiddenInventory = false;
  results.push("hidden-stock-not-displayed-as-zero");
  await page.setViewportSize({ width: 320, height: 740 });
  await page.goto(base + "/mobile/query");
  await page.getByRole("tab", { name: "认纸板" }).waitFor();
  await clean("07-small-phone", false);
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto(base + "/");
  await page.locator(".erp-shell").waitFor();
  assert.equal(await page.locator(".mobile-app").count(), 0);
  results.push("desktop-shell-preserved");
  await page.setViewportSize({ width: 375, height: 812 });
  await page.goto(base + "/");
  await page.locator(".mobile-app").waitFor();
  assert.equal(new URL(page.url()).pathname, basePath + "/mobile");
  results.push("phone-home-entry");
  unauthorized = true;
  await page.goto(base + "/mobile/query");
  await page.waitForURL("**/login**");
  assert.match(new URL(page.url()).pathname, /login/);
  results.push("expired-session-redirect");
  assert.deepEqual(consoleErrors, []);
  assert.deepEqual(networkLeaks, []);
  await fs.writeFile(
    path.join(out, "ui-result.json"),
    JSON.stringify(
      {
        status: "passed",
        browser: "isolated Google Chrome",
        checks: results,
        requests: requests.length,
        unexpectedPageErrors: consoleErrors,
        nonlocalRequests: networkLeaks,
        productionBusinessWrites: false,
      },
      null,
      2,
    ),
  );
  await browser.close();
  console.log(
    JSON.stringify({ status: "passed", checks: results.length, evidence: out }),
  );
}
run().catch(async (error) => {
  console.error(error);
  await activeBrowser?.close();
  process.exitCode = 1;
});
