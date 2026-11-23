import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "@sinclair/typebox";
import { execFile } from "child_process";
import { promisify } from "util";
import * as fs from "fs";
import * as path from "path";
import * as os from "os";

const execFileAsync = promisify(execFile);

export default function (pi: ExtensionAPI) {
  // Helper to find the absolute path of run.sh in the project workspace
  const getLauncherPath = (): string => {
    // Attempt to use current directory or locate run.sh in parent directories
    let dir = process.cwd();
    while (dir && dir !== path.parse(dir).root) {
      const launcher = path.join(dir, "ccworks");
      if (fs.existsSync(launcher)) {
        return launcher;
      }
      dir = path.dirname(dir);
    }
    return "./ccworks"; // Fallback to relative
  };

  const runCommand = async (args: string[]): Promise<string> => {
    const launcher = getLauncherPath();
    try {
      const { stdout, stderr } = await execFileAsync(launcher, args, { cwd: path.dirname(launcher) });
      return stdout + "\n" + stderr;
    } catch (error: any) {
      const err = new Error(`Execution failed: ${error.message}\nOutput: ${error.stdout || ""}\nError: ${error.stderr || ""}`, { cause: error });
      (err as any).code = error.code;
      throw err;
    }
  };

  // 1. Tool: concur_list_reports
  pi.registerTool({
    name: "concur_list_reports",
    label: "List Concur Reports",
    description: "Queries and lists active or historical expense reports in SAP Concur.",
    parameters: Type.Object({
      filter_view: Type.Optional(Type.String({
        description: "Dropdown filter to apply (e.g., 'Last 90 Days', 'All Reports', 'Active Reports')",
        default: "Last 90 Days"
      })),
      is_old: Type.Optional(Type.Boolean({
        description: "Set true to query historical reports, false for current active/draft reports",
        default: true
      }))
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const filter = params.filter_view || "Last 90 Days";
      const isOld = params.is_old !== false;
      
      const args = isOld ? ["report", "list", "--historical", "--view", filter] : ["report", "list"];
      const output = await runCommand(args);
      
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 2. Tool: concur_report_details
  pi.registerTool({
    name: "concur_report_details",
    label: "Get Concur Report Details",
    description: "Fetches full metadata and line-item details of a specific expense report by name.",
    parameters: Type.Object({
      report_name: Type.String({ description: "The exact name of the target expense report" }),
      filter_view: Type.Optional(Type.String({
        description: "The view filter to look inside (e.g., 'Last 90 Days', 'All Reports')",
        default: "Last 90 Days"
      }))
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const filter = params.filter_view || "Last 90 Days";
      const output = await runCommand(["report", "show", params.report_name, "--view", filter]);
      
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 3. Tool: concur_list_cards
  pi.registerTool({
    name: "concur_list_card_transactions",
    label: "List Card Transactions",
    description: "Queries and lists credit card transactions inside the Available Expenses section.",
    parameters: Type.Object({
      filter_view: Type.Optional(Type.String({
        description: "The activity filter to apply (e.g., 'All Corporate and Personal Cards', 'All Purchasing Cards')",
        default: "All Corporate and Personal Cards"
      }))
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const filter = params.filter_view || "All Corporate and Personal Cards";
      const output = await runCommand(["card", "list", "--view", filter]);
      
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 4. Tool: concur_reconcile_report
  pi.registerTool({
    name: "concur_reconcile_report",
    label: "Reconcile Concur Report",
    description: "Automates month-end reconciliation: fills in Expense Type, Purpose, Comment, and Allocation Codes for each transaction in the report, and optionally submits the report.",
    parameters: Type.Object({
      report_name: Type.String({ description: "Name of the draft expense report to reconcile" }),
      rules: Type.String({
        description: "JSON string representing mapping rules. Keys are merchant names (e.g., 'Uber'), values map to expense_type, business_purpose, comment, allocation_code."
      }),
      submit: Type.Optional(Type.Boolean({
        description: "Whether to submit the report after reconciling (default: false, leaving report in draft mode for review)",
        default: false
      }))
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      // Validate JSON rules
      let rulesObj;
      try {
        rulesObj = JSON.parse(params.rules);
      } catch (err: any) {
        throw new Error(`Invalid JSON rules input: ${err.message}`, { cause: err });
      }

      // Write rules temporarily to file
      const tempFile = path.join(os.tmpdir(), `concur-recon-${Date.now()}.json`);
      fs.writeFileSync(tempFile, JSON.stringify(rulesObj, null, 2));

      try {
        const cmdArgs = ["report", "reconcile", params.report_name, "--rules", tempFile];
        if (params.submit) {
          cmdArgs.push("--submit");
        }
        const output = await runCommand(cmdArgs);
        return {
          content: [{ type: "text", text: output }],
          details: {}
        };
      } finally {
        if (fs.existsSync(tempFile)) {
          fs.unlinkSync(tempFile);
        }
      }
    }
  });

  // 5. Tool: concur_attach_receipt
  pi.registerTool({
    name: "concur_attach_receipt",
    label: "Attach Receipt to Expense",
    description: "Attaches a local receipt image or PDF file directly to a transaction inside an expense report.",
    parameters: Type.Object({
      report_name: Type.String({ description: "Name of the expense report containing the transaction" }),
      merchant: Type.String({ description: "Merchant name or transaction ID to match receipt against (e.g., 'Uber')" }),
      receipt_path: Type.String({ description: "Absolute local path to the receipt file (PDF or image)" })
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const output = await runCommand([
        "txn", "attach-receipt",
        params.report_name,
        "--merchant", params.merchant,
        "--file", params.receipt_path
      ]);
      
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 6. Tool: concur_create_report
  pi.registerTool({
    name: "concur_create_report",
    label: "Create Draft Report",
    description: "Creates a new draft expense report headlessly.",
    parameters: Type.Object({
      name: Type.String({ description: "Name of the expense report to create" }),
      purpose: Type.Optional(Type.String({ description: "Business purpose of the report" })),
      comment: Type.Optional(Type.String({ description: "Optional comment or description for the report" }))
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const args = ["report", "create"];
      if (params.name) args.push("--name", params.name);
      if (params.purpose) args.push("--purpose", params.purpose);
      if (params.comment) args.push("--comment", params.comment);
      
      const output = await runCommand(args);
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 7. Tool: concur_delete_report
  pi.registerTool({
    name: "concur_delete_report",
    label: "Delete Draft Report",
    description: "Deletes a draft expense report by name.",
    parameters: Type.Object({
      report_name: Type.String({ description: "The exact name of the draft report to delete" })
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const output = await runCommand(["report", "delete", params.report_name]);
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 8. Tool: concur_card_transaction_details
  pi.registerTool({
    name: "concur_card_transaction_details",
    label: "Get Card Transaction Details",
    description: "Fetches details of a specific credit card transaction by merchant or ID.",
    parameters: Type.Object({
      merchant_or_id: Type.String({ description: "Merchant name or transaction ID to look up (e.g., 'Uber', 'TX_5002')" }),
      filter_view: Type.Optional(Type.String({
        description: "Dropdown filter to look inside (e.g., 'All Corporate and Personal Cards', 'All Purchasing Cards')",
        default: "All Corporate and Personal Cards"
      }))
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const filter = params.filter_view || "All Corporate and Personal Cards";
      const output = await runCommand(["card", "show", params.merchant_or_id, "--view", filter]);
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 9. Tool: concur_add_delegate
  pi.registerTool({
    name: "concur_add_delegate",
    label: "Add Expense Delegate",
    description: "Adds a new expense delegate in settings with specified permissions.",
    parameters: Type.Object({
      name_or_email: Type.String({ description: "Full name or email address of the delegate to add" }),
      permissions: Type.Optional(Type.Array(Type.String(), {
        description: "List of permissions to assign (e.g. ['prepare', 'submit', 'approve'])",
        default: ["prepare"]
      }))
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const perms = params.permissions || ["prepare"];
      const output = await runCommand(["delegate", "add", params.name_or_email, "--can", ...perms]);
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 10. Tool: concur_remove_delegate
  pi.registerTool({
    name: "concur_remove_delegate",
    label: "Remove Expense Delegate",
    description: "Removes an expense delegate from settings by name or email.",
    parameters: Type.Object({
      name_or_email: Type.String({ description: "Full name or email address of the delegate to remove" })
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const output = await runCommand(["delegate", "remove", params.name_or_email]);
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 11. Tool: concur_nuke_drafts_and_receipts
  pi.registerTool({
    name: "concur_nuke_drafts_and_receipts",
    label: "Nuke Drafts and Receipts",
    description: "Deletes all draft reports and available receipts inside Concur (intended for testing cleanup).",
    parameters: Type.Object({}),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      const output = await runCommand(["nuke"]);
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });

  // 12. Tool: concur_check_session
  pi.registerTool({
    name: "concur_check_session",
    label: "Check Concur Session Validity",
    description: "Checks whether the currently saved browser session state is active and valid (returns true if authenticated, false otherwise).",
    parameters: Type.Object({}),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      try {
        const output = await runCommand(["session", "status"]);
        return {
          content: [{ type: "text", text: output }],
          details: { valid: true }
        };
      } catch (error: any) {
        if (error.code === 2) {
          return {
            content: [
              { type: "text", text: `Session is invalid or expired.\n\n${error.message}` }
            ],
            details: { valid: false }
          };
        }
        throw error;
      }
    }
  });

  // 13. Tool: concur_update_transaction
  pi.registerTool({
    name: "concur_update_transaction",
    label: "Update Report Transaction Fields",
    description: "Updates fields (Expense Type, Business Purpose, and Comments) of an individual transaction inside an expense report, identified by its 0-based index. To remove a comment, pass an empty string.",
    parameters: Type.Object({
      report_name: Type.String({ description: "Name of the expense report containing the transaction" }),
      transaction_index: Type.Integer({ minimum: 0, description: "0-based index of the target transaction row" }),
      type: Type.Optional(Type.String({ description: "The Expense Type to apply" })),
      purpose: Type.Optional(Type.String({ description: "The Business Purpose to apply" })),
      comment: Type.Optional(Type.String({ description: "The Comment to apply (pass empty string to clear/delete)" }))
    }),
    async execute(toolCallId, params, signal, onUpdate, ctx) {
      if (!Number.isInteger(params.transaction_index) || params.transaction_index < 0) {
        throw new RangeError("transaction_index must be a non-negative integer");
      }
      const args = ["txn", "update", params.report_name, String(params.transaction_index + 1)];
      if (params.type !== undefined) {
        args.push("--type", params.type);
      }
      if (params.purpose !== undefined) {
        args.push("--purpose", params.purpose);
      }
      if (params.comment !== undefined) {
        args.push("--comment", params.comment);
      }
      const output = await runCommand(args);
      return {
        content: [{ type: "text", text: output }],
        details: {}
      };
    }
  });
}
