// Run: node --experimental-vm-modules --test tests/test_extension.mjs
// Exercise the real extension with a fake launcher and no Concur credentials.
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import vm from "node:vm";
import { stripTypeScriptTypes } from "node:module";
import { after, test } from "node:test";

const originalCwd = process.cwd();
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "ccworks-audit-"));
fs.writeFileSync(
  path.join(tmp, "ccworks"),
  "#!/bin/sh\nprintf '%s\\n' \"$@\"\n",
  { mode: 0o755 },
);
after(() => {
  process.chdir(originalCwd);
  fs.rmSync(tmp, { recursive: true });
});

const source = fs.readFileSync(
  new URL("../.pi/extensions/concur.ts", import.meta.url),
  "utf8",
);
const extension = new vm.SourceTextModule(stripTypeScriptTypes(source));
await extension.link(async (specifier) => {
  // The schema library is irrelevant to argument forwarding. All operating
  // system modules are real, including child_process.
  const exports =
    specifier === "@sinclair/typebox"
      ? {
          Type: new Proxy(
            {},
            {
              get:
                () =>
                (...args) =>
                  args,
            },
          ),
        }
      : await import(specifier);
  return new vm.SyntheticModule(Object.keys(exports), function () {
    for (const [name, value] of Object.entries(exports))
      this.setExport(name, value);
  });
});
await extension.evaluate();
const registered = new Map();
extension.namespace.default({
  registerTool: (tool) => registered.set(tool.name, tool),
});
process.chdir(tmp);

test("report names reach the launcher literally without shell substitution", async () => {
  for (const name of [
    "$(printf audit-shell-expanded)",
    "`printf backtick-expanded`",
    "$HOME",
    'a "quoted" name; value',
  ]) {
    const result = await registered
      .get("concur_report_details")
      .execute("audit", { report_name: name });
    assert.equal(result.content[0].text.split("\n")[2], name);
  }
});

test("extension 0-based transaction index is converted for the 1-based CLI", async () => {
  for (const index of [0, 1, 5]) {
    const result = await registered
      .get("concur_update_transaction")
      .execute("audit", {
        report_name: "Statement",
        transaction_index: index,
        comment: "expense edit",
      });
    assert.equal(result.content[0].text.split("\n")[3], String(index + 1));
  }
});

test("invalid indices are rejected before launching a command", async () => {
  for (const index of [-1, 1.5, "1"]) {
    await assert.rejects(
      registered.get("concur_update_transaction").execute("audit", {
        report_name: "Statement",
        transaction_index: index,
      }),
      /non-negative integer/,
    );
  }
});
