const assert = require("node:assert/strict");
const fs = require("node:fs");

const html = fs.readFileSync("static/index.html", "utf8");
const methodsStart = html.indexOf("methods: {");
assert.notEqual(methodsStart, -1, "missing Vue methods block");

function extractMethod(name) {
  const match = new RegExp(`^          (?:async )?${name}\\(`, "m").exec(html.slice(methodsStart));
  const start = match ? methodsStart + match.index + match[0].indexOf(name) : -1;
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
  ["bomComponentSpecification", "bomComponentUnit", "bomRelationshipQuantity", "bomRelationshipSummary"].map((name) => [name, extractMethod(name)]),
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

assert.equal(context.bomRelationshipSummary(), "每1只用量：2只 长边 + 3片 短边");
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
assert.equal(context.bomRelationshipSummary(), "每1只用量：2只 长边（可选）");
assert.match(html, /component\.is_required === false \? '可选' : '必需'/);
console.log("BOMUI001 dynamic relationship summary passed");

const extra = Function(`return ({${["bomComponentOption", "mergeBomComponentOptions", "selectBomComponent", "bomInventoryModeHint"].map(extractMethod).join(",")}});`)();
Object.assign(context, extra);
context.productForm = {id:100,unit:"套"};
context.bomEditor = {inventory_mode:"assembled",componentOptions:[],components:[
 {component_product_id:11,quantity_per_set:3,unit:"旧单位",component:{id:11,unit:"片"},product_name:"长片15片"},
 {component_product_id:12,quantity_per_set:4,unit:"片",product_name:"短片20片"}
]};
context.bomEditor.components.forEach(row=>row.inventory_relation="assembly");
assert.equal(context.bomRelationshipSummary(),"消耗 3片 长片15片 + 4片 短片20片 → 组装1套");
context.mergeBomComponentOptions(context.bomEditor.components);
assert.equal(context.bomEditor.componentOptions[0].unit,"片");
context.selectBomComponent(context.bomEditor.components[0],11);
assert.equal(context.bomEditor.components[0].unit,"片");
context.bomEditor.components[0].quantity_per_set=5;
assert.match(context.bomRelationshipSummary(),/消耗 5片/);
context.productForm.unit="只";
context.bomEditor.inventory_mode="manufactured";
context.bomEditor.components=[{component_product_id:100,quantity_per_set:5,unit:"套",product_name:"格挡",inventory_relation:"accompany"}];
assert.equal(context.bomRelationshipSummary(),"每1只随货配 5套 格挡");
assert.match(html,/row\.component=data; row\.unit=data\.unit/);
assert.match(html,/每父件子件用量/);
assert.match(html,/子件资料<\/button>/);
console.log("BOMUI002 unit roundtrip, recipe edit and relation semantics passed");
