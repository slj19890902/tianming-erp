const assert = require("node:assert/strict");
const { render, number } = require("../static/assets/order-early-pick-print.js");
assert.equal(number(222), "222");
assert.equal(number(222.5), "222.500");
assert.equal(number(222.525), "222.525");
assert.equal(number(null), "待核对");
const data = {
  customer_name: "<script>alert('x')</script>", order_number: "SO-1", customer_po: null,
  delivery_date: "2026-10-20", source_name: "10.20.xlsx", generated_at: "2026-10-20T00:00:00Z",
  notice: "只读备货", rows: Array.from({ length: 45 }, (_, i) => ({
    sequence: i + 1, product_code: `8001${String(i).padStart(4, "0")}`, product_name: "测试产品",
    unit: "片", requested_location: i === 1 ? "板" : "A6-2", order_quantity: 450,
    delivered_quantity: 0, available_quantity: i === 1 ? null : 120,
    shortage_quantity: i === 1 ? null : 330, notes: ["待生产"], locations: [],
  })),
};
const html = render(data);
assert.equal((html.match(/<tr>/g) || []).length, 46);
assert.ok(html.indexOf("80010000") < html.indexOf("80010044"));
assert.ok(html.includes("位置待核对"));
assert.ok(html.includes("待核对"));
assert.ok(!html.includes("<script>"));
assert.ok(html.includes("&lt;script&gt;"));
assert.ok(!html.includes("单价"));
console.log("PASS 45-row early picking print, escaping, row order, quantities and price-free output");
