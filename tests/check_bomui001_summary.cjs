const assert = require("node:assert/strict");
const fs = require("node:fs");

const html = fs.readFileSync("static/index.html", "utf8");
const methodsStart = html.indexOf("methods: {");
assert.notEqual(methodsStart, -1, "missing Vue methods block");

function extractMethod(name) {
  const start = html.indexOf(`${name}(`, methodsStart);
  assert.notEqual(start, -1, `missing ${name}`);
  const open = html.indexOf("{", start);
  let depth = 0;
  let quote = null;
  let escaped = false;
  for (let index = open; index < html.length; index += 1) {
    const character = html[index];
    if (quote) {
      if (escaped) {
        escaped = false;
      } else if (character === "\\") {
        escaped = true;
      } else if (character === quote) {
        quote = null;
      }
      continue;
    }
    if (["'", '"', "`"].includes(character)) {
      quote = character;
      continue;
    }
    if (character === "{") depth += 1;
    if (character === "}") {
      depth -= 1;
      if (depth === 0) return html.slice(start, index + 1);
    }
  }
  throw new Error(`unterminated ${name}`);
}

const methods = Object.fromEntries(
  ["bomComponentSpecification", "bomRelationshipQuantity", "bomRelationshipSummary"].map((name) => [name, extractMethod(name)]),
);
const methodObject = Function(`return ({${Object.values(methods).join(",")}});`)();
const context = {
  productForm: { unit: "只" },
  bomEditor: {
    components: [
      { component_product_id: 11, quantity_per_set: 2, unit: "只", product_code: "A-01", product_name: "长边" },
      { component_product_id: 12, quantity_per_set: 3, unit: "片", product_code: "B-02", product_name: "短边" },
    ],
  },
  spec: () => "-",
};
Object.assign(context, methodObject);

assert.equal(context.bomRelationshipSummary(), "1只 = 2只 A-01｜长边 + 3片 B-02｜短边");
assert.equal(context.bomRelationshipQuantity(2.5), "2.5");
assert.equal(context.bomRelationshipQuantity(0), "数量待完善");

assert.equal(
  context.bomComponentSpecification({specification: "旧快照", component: {specification: "当前组件规格"}}),
  "当前组件规格",
  "loaded component identity must win over stale top-level snapshot",
);
assert.equal(
  context.bomComponentSpecification({specification: "仅旧快照"}),
  "仅旧快照",
  "unloaded component may use its snapshot fallback",
);
context.bomEditor.components = [{component_product_id: null, quantity_per_set: 1}];
assert.match(context.bomRelationshipSummary(), /待完善/);
context.bomEditor.components = [{component_product_id: 11, quantity_per_set: 2, unit: "只", product_code: "A-01", product_name: "长边", is_required: false}];
assert.match(html, /component\.is_required === false \? '可选' : '必需'/);
console.log("BOMUI001 dynamic relationship summary passed");
