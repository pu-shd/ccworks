import os
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# Global states
REPORTS = []
RECEIPTS = []
DELEGATES = [
    {"name": "Existing Delegate", "email": "existing@example.com", "prepare": True, "submit": False, "approve": False}
]

DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>SAP Concur Expense Dashboard</title>
    <style>
        body { font-family: sans-serif; padding: 20px; background-color: #f7f9fa; }
        .section-title { margin-top: 30px; border-bottom: 2px solid #e0e5ea; padding-bottom: 5px; color: #1a1a1a; }
        .report-card { border: 1px solid #e1e4e6; border-radius: 8px; background: white; padding: 15px; margin: 15px 0; display: flex; justify-content: space-between; align-items: center; box-shadow: 0 2px 4px rgba(0,0,0,0.05); cursor: pointer; }
        .report-info { flex: 1; }
        .report-name { font-weight: bold; font-size: 1.1em; color: #0070d2; text-decoration: underline; }
        .report-purpose { color: #5c646b; margin-left: 10px; font-style: italic; }
        .button { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; font-size: 0.9em; }
        #create-report-btn { background-color: #0070d2; color: white; margin-bottom: 10px; }
        .edit-btn { background-color: #e0e5ea; color: #0070d2; margin-right: 10px; }
        .delete-btn { background-color: #c23934; color: white; }
        #report-dialog { border: 1px solid #c9c9c9; border-radius: 6px; padding: 25px; position: absolute; background: white; top: 100px; left: 100px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); width: 350px; z-index: 1000; }
        .form-group { margin-bottom: 15px; }
        .form-group label { display: block; margin-bottom: 5px; font-weight: bold; }
        .form-group input, .form-group textarea, .form-group select { width: 95%; padding: 8px; border: 1px solid #c9c9c9; border-radius: 4px; }
        .form-actions { display: flex; justify-content: flex-end; }
        .form-actions button { margin-left: 10px; }
        
        /* Available Receipts / Transactions Gallery */
        .receipt-gallery { display: flex; gap: 15px; flex-wrap: wrap; margin-top: 15px; }
        .available-receipt-thumbnail { border: 1px solid #c9c9c9; border-radius: 6px; padding: 15px; width: 120px; text-align: center; background: white; cursor: pointer; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .available-receipt-thumbnail:hover { border-color: #0070d2; }
        .card-transaction-thumbnail { border: 1px solid #c9c9c9; border-radius: 6px; padding: 15px; width: 120px; text-align: center; background: white; cursor: pointer; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .card-transaction-thumbnail:hover { border-color: #0070d2; }
        .receipt-icon { font-size: 2em; display: block; margin-bottom: 5px; }
        .receipt-name { font-size: 0.85em; word-break: break-all; font-weight: bold; color: #1a1a1a; }
        #receipt-modal { border: 1px solid #c9c9c9; border-radius: 6px; padding: 25px; position: absolute; background: white; top: 150px; left: 150px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); width: 300px; z-index: 1001; text-align: center; }
        
        /* Transaction Details Modal */
        #transaction-modal { border: 1px solid #c9c9c9; border-radius: 6px; padding: 25px; position: absolute; background: white; top: 200px; left: 200px; box-shadow: 0 4px 12px rgba(0,0,0,0.15); width: 320px; z-index: 1002; }
        
        /* Report Details View Panel */
        #report-details-panel { display: none; background: white; padding: 20px; border: 1px solid #e1e4e6; border-radius: 8px; margin-top: 20px; }
        .detail-row { margin-bottom: 10px; border-bottom: 1px solid #eee; padding-bottom: 5px; }

        /* Per-row receipt controls. Real Concur exposes no <input type="file">
           in the grid: attaching opens the OS file chooser. The controls below
           mirror the live attributes (data-nuiexp / data-nui-widgets / aria-label)
           and route through a single page-level hidden input purely so that
           Playwright's expect_file_chooser has something to intercept. */
        .rcpt-attach, .rcpt-thumb, .row-actions-trigger { border: 1px solid #c9c9c9; background: white; border-radius: 4px; cursor: pointer; padding: 2px 6px; font-size: 12px; }
        .row-actions-wrap { position: relative; }
        .row-actions-menu { position: absolute; top: 20px; left: 0; background: white; border: 1px solid #c9c9c9; border-radius: 4px; box-shadow: 0 2px 6px rgba(0,0,0,0.15); z-index: 6000; min-width: 140px; }
        .row-actions-menu [role="menuitem"] { padding: 8px 12px; cursor: pointer; font-size: 12px; white-space: nowrap; }
        .row-actions-menu [role="menuitem"]:hover { background: #e0e5ea; }
    </style>
</head>
<body>
    <!--
      Reproduction of the onboarding dialog Concur shows over the dashboard
      after a feature release. Class names, role, and aria-modal are copied
      from a real failure where clicking a report tile timed out for 30s with
      "<div class='sapcnqr-dialog__body ...'> subtree intercepts pointer
      events". It is rendered on every dashboard load so all browser tests
      exercise the dismissal in _wait_for_dashboard.
    -->
    <div id="onboarding-dialog"
         role="dialog" tabindex="-1" aria-modal="true" aria-labelledby="onboarding-title"
         class="sapcnqr-dialog vip-widgets__text-app-onboarding-dialog sapcnqr-dialog--width-lg sapcnqr-dialog__fade sapcnqr-dialog__fade--in"
         style="position:fixed; inset:0; z-index:9999; display:flex; align-items:center; justify-content:center;">
      <div class="sapcnqr-dialog__body vip-widgets__text-app-onboarding-dialog__body"
           style="position:absolute; inset:0; background:rgba(0,0,0,0.4);">
        <div style="background:#fff; padding:24px; max-width:420px; margin:120px auto; border-radius:8px;">
          <h2 id="onboarding-title">What's New in Expense</h2>
          <p>Take a quick tour of the redesigned expense experience.</p>
          <button aria-label="Close" onclick="dismissOnboarding()">Close</button>
        </div>
      </div>
    </div>
    <script>
        function dismissOnboarding() {
            var el = document.getElementById("onboarding-dialog");
            if (el) { el.remove(); }
        }
    </script>

    <h1>Expense Dashboard</h1>

    <div style="margin-bottom: 15px;">
        <label for="report-view-select" style="font-weight:bold;">View: </label>
        <!-- The View select dropdown to filter Reports -->
        <select id="report-view-select" style="width: 200px; padding: 5px;" onchange="changeReportView(this.value)">
            <option value="Active Reports">Active Reports</option>
            <option value="Last 90 Days">Last 90 Days</option>
            <option value="All Reports">All Reports</option>
        </select>
    </div>

    <h2 class="section-title">Expense Reports</h2>
    <button id="create-report-btn" class="button" onclick="showCreateModal()">Create New Report</button>
    <div id="reports-container"></div>

    <!-- Allocations modal. Mirrors the live flow: row kebab -> Allocate ->
         a list plus an Add form whose chartstring fields are the same combobox
         widget as the expense type (field-customN / __trigger / __input). -->
    <div id="allocations-modal" style="display:none; position:absolute; top:120px; left:120px;
         background:white; border:1px solid #c9c9c9; padding:20px; z-index:4000; width:420px;">
        <h3>Allocations</h3>
        <!-- .allocation-grid-container and per-row checkboxes mirror the live
             grid: clearing an allocation means ticking rows and pressing
             Remove, which is the only way to replace a chartstring since Add
             splits the expense by percentage instead of superseding it. -->
        <div class="allocation-grid-container"><div id="allocations-list"></div></div>
        <button type="button" data-nuiexp="allocations-addBtn" onclick="showAddAllocation()">Add</button>
        <button type="button" data-nuiexp="allocations-removeBtn" onclick="removeCheckedAllocations()">Remove</button>
        <div id="add-allocation-form" style="display:none; margin-top:12px;">
            <div id="alloc-field-custom6"></div>
            <div id="alloc-field-custom7"></div>
            <div id="alloc-field-custom8"></div>
            <button type="button" data-nuiexp="Ct-add-btn" onclick="saveAddAllocation()">Save</button>
        </div>
        <div style="margin-top:12px;">
            <button type="button" data-nuiexp="allocation-modal-save"
                    onclick="saveAllocations()">Save</button>
            <button type="button" onclick="closeAllocations()">Cancel</button>
        </div>
        <div id="alloc-popup" class="sapcnqr-overlay__dialog" style="display:none; background:white;
             border:1px solid #c9c9c9; padding:6px; z-index:5000;">
            <input type="text" id="alloc-search" style="width:100%;"
                   placeholder="Search" oninput="renderAllocOptions(this.value)">
            <div class="sapcnqr-selection-list__list-box" role="listbox" id="alloc-listbox"></div>
        </div>
    </div>

    <!-- Report Details Panel -->
    <div id="report-details-panel">
        <h2 id="detail-header-title">Report Detail</h2>
        <div style="margin-bottom: 10px;">
            <button id="report-details-btn" class="button" style="background:#e0e5ea;" onclick="showEditModalForCurrentReport()">Report Details</button>
        </div>
        <p><strong>Report Number / ID:</strong> <span id="detail-report-id">REP-998877</span></p>
        <p><strong>Purpose:</strong> <span id="detail-purpose"></span></p>
        <p><strong>Comment:</strong> <span id="detail-comment"></span></p>
        
        <h3>Expenses (Line Items)</h3>
        <div id="detail-expenses-list">
            <!-- Dynamically populated -->
        </div>
        <div id="report-detail-actions" style="margin-top:15px;">
            <button class="button" onclick="closeReportDetails()" style="background:#e0e5ea; margin-right: 10px;">Back to List</button>
            <button id="edit-transaction-btn" class="button" style="background:#0070d2; color:white; display:none;" onclick="openTransactionDetail()">Edit</button>
            <button id="submit-entire-report-btn" class="button" style="background-color: #c9c9c9; color: white;" disabled onclick="submitReport()">Submit Report</button>
        </div>
    </div>

    <!-- Transaction Detail Side Panel (Simulated) -->
    <div id="sapcnqr-layout-side-panel-elements" class="sapcnqr-layout-side-panel__elements ere__dynamic-main-content" style="display:none; position:fixed; right:0; top:0; width:400px; height:100%; background:white; border-left:1px solid #ccc; padding:20px; box-shadow:-2px 0 5px rgba(0,0,0,0.1); z-index:5000;">
        <h2>Expense Details</h2>
        <div id="detail-pane-content"></div>
    </div>

    <h2 class="section-title">Available Expenses (Card Transactions)</h2>
    <div style="margin-bottom: 15px;">
        <label for="card-view-select" style="font-weight:bold;">Activity: </label>
        <!-- Card views filter dropdown -->
        <select id="card-view-select" style="width: 250px; padding: 5px;" onchange="changeCardView(this.value)">
            <option value="All Corporate and Personal Cards">All Corporate and Personal Cards</option>
            <option value="All Purchasing Cards">All Purchasing Cards</option>
        </select>
    </div>
    <div id="cards-container" class="receipt-gallery"></div>

    <h2 class="section-title">Available Receipts</h2>
    <div id="receipts-container" class="receipt-gallery"></div>

    <!-- Create/Edit Modal Dialog -->
    <div id="report-dialog" style="display:none;">
        <h2 id="dialog-title">Create Report</h2>
        <form onsubmit="saveReport(event)">
            <input type="hidden" id="edit-index">
            <div class="form-group">
                <label for="reportname">Report Name</label>
                <input type="text" id="reportname" required>
            </div>
            <div class="form-group">
                <label for="purpose">Purpose</label>
                <input type="text" id="purpose">
            </div>
            <div class="form-group">
                <label for="comment">Comment</label>
                <textarea id="comment"></textarea>
            </div>
            <div class="form-actions">
                <button type="button" class="button" onclick="closeDialog()">Cancel</button>
                <button type="submit" id="submit-report-btn" class="button">Create Report</button>
            </div>
        </form>
    </div>

    <!-- Receipt Viewer Dialog -->
    <div id="receipt-modal" style="display:none;">
        <h2>Receipt Viewer</h2>
        <div style="padding: 10px; background: #f0f0f0; border: 1px dashed #ccc; margin-bottom: 15px;">
            <span style="font-size: 3em;">📄</span>
        </div>
        <p id="receipt-modal-name" class="receipt-name"></p>
        <div style="margin-top: 20px;">
            <button type="button" class="button" onclick="closeReceiptModal()" style="margin-right:10px;">Close</button>
            <button type="button" id="delete-receipt-btn" class="button delete-btn" onclick="triggerDeleteReceipt()">Delete Receipt</button>
        </div>
    </div>

    <!-- Card Transaction Details Dialog -->
    <div id="transaction-modal" style="display:none;">
        <h2>Card Transaction Details</h2>
        <p><strong>Merchant:</strong> <span id="tx-merchant"></span></p>
        <p><strong>Date:</strong> <span id="tx-date"></span></p>
        <p><strong>Amount:</strong> <span id="tx-amount"></span></p>
        <p><strong>Transaction ID:</strong> <span id="tx-id"></span></p>
        <p><strong>Card Program:</strong> <span id="tx-program"></span></p>
        <div style="margin-top: 20px;">
            <button type="button" class="button" onclick="closeTxModal()">Close</button>
        </div>
    </div>

    <script>
        let currentReportView = "Active Reports";
        let currentCardView = "All Corporate and Personal Cards";
        let reportsData = [];
        
        // Static historical reports
        const historicalReports = [
            { name: "Old Lodging Report 2025", purpose: "FY2025 Conference", comment: "Approved & Paid", id: "REP-100200" },
            { name: "Q1 Travel Report", purpose: "Client Visits Q1", comment: "Payment Completed", id: "REP-300400" }
        ];

        // Static credit card transactions
        const cardTransactions = [
            { id: "TX_5001", merchant: "Uber Rides", date: "2026-06-15", amount: "$24.50", program: "Corporate Card" },
            { id: "TX_5002", merchant: "Office Depot", date: "2026-06-18", amount: "$189.99", program: "Purchasing Card" },
            { id: "TX_5003", merchant: "Starbucks Breakfast", date: "2026-06-20", amount: "$8.75", program: "Personal Card" }
        ];

        async function fetchReports() {
            const res = await fetch('/api/reports');
            reportsData = await res.json();
            renderReports();
        }

        async function fetchReceipts() {
            const res = await fetch('/api/receipts');
            const receipts = await res.json();
            renderReceipts(receipts);
        }

        function changeReportView(val) {
            currentReportView = val;
            renderReports();
        }

        function changeCardView(val) {
            currentCardView = val;
            renderTransactions();
        }

        function renderReports() {
            const container = document.getElementById('reports-container');
            container.innerHTML = '';
            
            let list = [...reportsData];
            if (currentReportView === "Last 90 Days" || currentReportView === "All Reports") {
                list = list.concat(historicalReports);
            }

            if (list.length === 0) {
                container.innerHTML = '<p class="no-reports">No reports found.</p>';
                return;
            }

            list.forEach((r, idx) => {
                const card = document.createElement('div');
                card.className = 'report-card report-tile';
                // Open details on clicking the card info (except edit/delete buttons)
                card.onclick = (e) => {
                    if (e.target.tagName !== 'BUTTON') {
                        showReportDetails(r);
                    }
                };
                card.innerHTML = `
                    <div class="report-info">
                        <span class="report-name">${r.name}</span>
                        <span class="report-purpose">(${r.purpose || 'No Purpose'})</span>
                        <p style="margin: 5px 0 0 0; font-size:12px; color:#5c646b;">Comment: ${r.comment || 'None'}</p>
                    </div>
                    <div>
                        ${r.id ? '<span style="font-weight:bold; color:green; margin-right:10px;">Submitted</span>' : `
                            <button class="button edit-btn" onclick="showEditModal(${idx}, '${r.name}', '${r.purpose || ''}', '${r.comment || ''}')">Modify</button>
                            <button class="button delete-btn" onclick="deleteReport('${r.name}')">Delete</button>
                        `}
                    </div>
                `;
                container.appendChild(card);
            });
        }

        function renderReceipts(receipts) {
            const container = document.getElementById('receipts-container');
            container.innerHTML = '';
            if (receipts.length === 0) {
                container.innerHTML = '<p class="no-reports">No available receipts.</p>';
                return;
            }
            receipts.forEach((r) => {
                const thumb = document.createElement('div');
                thumb.className = 'available-receipt-thumbnail';
                thumb.onclick = () => showReceiptModal(r.name);
                thumb.innerHTML = `
                    <span class="receipt-icon">📄</span>
                    <span class="receipt-name">${r.name}</span>
                `;
                container.appendChild(thumb);
            });
        }

        function renderTransactions() {
            const container = document.getElementById('cards-container');
            container.innerHTML = '';
            
            let filtered = [];
            if (currentCardView === "All Purchasing Cards") {
                filtered = cardTransactions.filter(t => t.program === "Purchasing Card");
            } else if (currentCardView === "All Corporate and Personal Cards") {
                filtered = cardTransactions.filter(t => t.program === "Corporate Card" || t.program === "Personal Card");
            }

            if (filtered.length === 0) {
                container.innerHTML = '<p class="no-reports">No transactions found.</p>';
                return;
            }

            filtered.forEach(t => {
                const thumb = document.createElement('div');
                thumb.className = 'card-transaction-thumbnail card-transaction-row';
                thumb.onclick = () => showTxModal(t);
                thumb.innerHTML = `
                    <span class="receipt-icon">💳</span>
                    <span class="receipt-name">${t.merchant}</span>
                    <span style="font-size: 0.8em; color: green; font-weight:bold; display:block;">${t.amount}</span>
                `;
                container.appendChild(thumb);
            });
        }

        // Details Panel Functions
        function showReportDetails(r) {
            document.getElementById('detail-header-title').innerText = r.name;
            document.getElementById('detail-report-id').innerText = r.id || "REP-DRAFT-8899";
            document.getElementById('detail-purpose').innerText = r.purpose || "N/A";
            document.getElementById('detail-comment').innerText = r.comment || "N/A";
            
            const list = document.getElementById('detail-expenses-list');
            list.innerHTML = '';
            
            const txs = r.transactions || [];
            if (txs.length === 0) {
                list.innerHTML = `
                    <div class="detail-row"><strong>Date:</strong> 2026-06-12 | <strong>Type:</strong> Lodging | <strong>Amount:</strong> $150.00 | <strong>Merchant:</strong> Hilton</div>
                    <div class="detail-row"><strong>Date:</strong> 2026-06-13 | <strong>Type:</strong> Meal | <strong>Amount:</strong> $45.20 | <strong>Merchant:</strong> Italian Bistro</div>
                `;
                const actionContainer = document.getElementById('report-detail-actions');
                actionContainer.innerHTML = `<button class="button" onclick="closeReportDetails()" style="margin-top:15px; background:#e0e5ea;">Back to List</button>`;
            } else {
                txs.forEach((t, idx) => {
                    const row = document.createElement('div');
                    row.id = `tx-row-${idx}`;
                    row.className = 'detail-row transaction-recon-row sapMLIB';
                    row.style.display = 'flex';
                    row.style.gap = '10px';
                    row.style.alignItems = 'center';
                    row.style.marginBottom = '10px';
                    row.style.cursor = 'pointer';
                    row.onclick = () => selectTransaction(row, t, idx);
                    // Grid-level receipt indicator only. Attaching happens in the
                    // detail pane, not here: the grid thumbnail can also denote a
                    // card e-receipt, which is not an uploaded file.
                    const receiptControl = t.receipt
                        ? `<button type="button" class="rcpt-thumb"
                                   data-nuiexp="receipt-thumbnail-button-${idx}"
                                   aria-label="View receipt">&#128220;</button>`
                        : '';
                    row.innerHTML = `
                        <div class="sapMCb" style="width:20px; height:20px; border:1px solid #ccc; margin-right:5px;"></div>
                        <div style="display:none;">Select expense</div>
                        <div style="width: 120px; font-weight: bold;" class="recon-merchant">${t.merchant} (${t.amount})</div>
                        <div style="display: flex; flex-direction: column; font-size: 11px;">
                            ${t.reconciled ? '<span style="color: green; font-weight: bold;" class="recon-status">✓ Reconciled</span>' : '<span style="color: red;" class="recon-status">Pending</span>'}
                            ${t.receipt ? `<span style="color: blue;" class="receipt-attached-name">Attached: ${t.receipt}</span>` : ''}
                        </div>
                        ${receiptControl}
                        <div class="row-actions-wrap">
                            <button type="button" class="row-actions-trigger"
                                    data-nui-widgets="menu-button-trigger" aria-label="Actions"
                                    onclick="event.stopPropagation(); toggleRowMenu(${idx});">&#8942;</button>
                            <div class="row-actions-menu" id="row-menu-${idx}" role="menu" style="display:none;">
                                <div role="menuitem" tabindex="0"
                                     onclick="event.stopPropagation(); openAllocations(${idx});">Allocate</div>
                            </div>
                        </div>
                    `;
                    list.appendChild(row);
                });
                
                updateActionButtons(r);
            }
            
            document.getElementById('reports-container').style.display = 'none';
            document.getElementById('report-details-panel').style.display = 'block';
        }

        let selectedTx = null;
        let selectedTxIdx = -1;
        let selectedReportName = '';

        function selectTransaction(row, t, idx) {
            // Deselect others
            document.querySelectorAll('.transaction-recon-row').forEach(r => {
                r.classList.remove('sapMLIBSelected');
                r.querySelector('.sapMCb').classList.remove('sapMCbMarkChecked');
            });
            
            row.classList.add('sapMLIBSelected');
            row.querySelector('.sapMCb').classList.add('sapMCbMarkChecked');
            selectedTx = t;
            selectedTxIdx = idx;
            document.getElementById('edit-transaction-btn').style.display = 'inline-block';
            openTransactionDetail();
        }

        function updateActionButtons(r) {
            const allReconciled = (r.transactions || []).every(t => t.reconciled);
            const submitBtn = document.getElementById('submit-entire-report-btn');
            submitBtn.disabled = !allReconciled;
            submitBtn.style.backgroundColor = allReconciled ? '#0070d2' : '#c9c9c9';
            selectedReportName = r.name;
        }

        function openTransactionDetail() {
            const t = selectedTx;
            const idx = selectedTxIdx;
            const pane = document.getElementById('sapcnqr-layout-side-panel-elements');
            const content = document.getElementById('detail-pane-content');
            
            content.innerHTML = `
                <div class="form-group">
                    ${renderExpenseTypeField(t, idx)}
                </div>
                <div class="form-group">
                    <label for="businessPurpose">Business Purpose</label>
                    <input type="text" class="recon-purpose" style="width: 100%;" id="businessPurpose" data-nuiexp="field-businessPurpose" value="${t.business_purpose || ''}">
                </div>
                <div class="form-group">
                    <label for="recon-comment">Comment</label>
                    <textarea class="recon-comment" style="width: 100%; height:80px;" id="recon-comment" data-nuiexp="field-comment">${t.comment || ''}</textarea>
                </div>
                <div class="form-group">
                    <label>Allocation Code</label>
                    <input type="text" id="recon-allocation-${idx}" style="width: 100%;" value="${t.allocation_code || ''}">
                </div>
                <div class="form-group">
                    <label>Receipt</label>
                    <!-- Receipt area. Rendered as a skeleton first and hydrated
                         asynchronously, exactly as live Concur does: reading the
                         controls before it settles is a race that silently
                         attaches nothing. -->
                    <div id="receipt-area"><div class="entry-receipts-accessible-skeleton" aria-live="polite"></div></div>
                </div>
                <div style="margin-top:20px;">
                    <button class="button" style="background:#e0e5ea;" onclick="closeTransactionDetail()">Cancel</button>
                    <button class="button" style="background:#04844b; color:white;" data-nuiexp="exp-save-expense" onclick="saveReconTransaction('${selectedReportName}', ${idx})">Save Expense</button>
                </div>
            `;
            pane.style.display = 'block';
            receiptTab = 'CARD';
            hydrateReceiptArea();
        }

        function answerUpdateOther(propagate) {
            closeSaveDialog();
            window.__updateAnswered = true;
            // `propagate` would also rewrite the allocation rows' own text.
            // ccworks answers "Do Not Update", so that path is left unexercised
            // rather than faked.
            const pending = window.__pendingSave || [];
            if (pending.length === 2) saveReconTransaction(pending[0], pending[1]);
        }

        // ---- Allocations ----------------------------------------------------
        // Chartstring fields use the same combobox widget as the expense type,
        // so production drives them with the same verified writer. The values
        // below are the real-world shape: a numeric department and a fund code.
        const ALLOC_OPTIONS = {
            custom6: [["(25605) ORF-Technical Support", ""], ["(25601) ORF-Administration", ""]],
            custom7: [["(A0001) General Fund", ""], ["(B0002) Sponsored Research", ""]],
            custom8: [["(P999) Research", ""], ["(P100) Teaching", ""]]
        };
        let allocIdx = -1, allocDraft = {}, allocActiveField = null;

        // The grid is still re-rendering just after an expense pane is saved, so
        // the row menu's contents are not there the instant the kebab is clicked.
        // Production checked once after a fixed 1s wait and gave up, which failed
        // every row of a combined write while the standalone command -- which
        // opens the report fresh -- worked. The delay below reproduces that.
        let menuSettleUntil = 0;

        function delayRowMenus(ms) {
            menuSettleUntil = Date.now() + ms;
        }

        function toggleRowMenu(idx) {
            const m = document.getElementById(`row-menu-${idx}`);
            if (!m) return;
            const wait = Math.max(0, menuSettleUntil - Date.now());
            if (wait > 0) {
                // Opens, but empty, exactly as the live grid behaves mid-render.
                m.style.display = 'block';
                const items = m.querySelectorAll('[role="menuitem"]');
                items.forEach(i => { i.style.display = 'none'; });
                setTimeout(() => items.forEach(i => { i.style.display = ''; }), wait);
                return;
            }
            m.querySelectorAll('[role="menuitem"]').forEach(i => { i.style.display = ''; });
            m.style.display = m.style.display === 'block' ? 'none' : 'block';
        }

        function openAllocations(idx) {
            allocIdx = idx;
            allocDraft = {};
            toggleRowMenu(idx);
            document.getElementById('add-allocation-form').style.display = 'none';
            document.getElementById('allocations-modal').style.display = 'block';
            renderAllocationsList();
        }

        function closeAllocations() {
            document.getElementById('allocations-modal').style.display = 'none';
            document.getElementById('alloc-popup').style.display = 'none';
        }

        function currentAllocations() {
            const r = reportsData.find(x => x.name === selectedReportName);
            const t = r && (r.transactions || [])[allocIdx];
            return (t && t.allocations) || [];
        }

        function renderAllocationsList() {
            const list = document.getElementById('allocations-list');
            const rows = currentAllocations();
            if (!rows.length) {
                list.innerHTML = '<div>No Allocations</div>';
                return;
            }
            // Header row carries the select-all box and the sort affordances, and
            // must not be counted as an allocation.
            const header = '<div role="row" class="alloc-header">'
                + '<input type="checkbox" id="alloc-select-all" onclick="toggleAllAllocs(this)">'
                + 'Select all rows DepartmentSort column ascendingCodeSort column ascending</div>';
            list.innerHTML = header + rows.map((a, i) =>
                `<div role="row" class="sapMLIB"><input type="checkbox" class="alloc-row-box"
                     data-i="${i}"> Select row ${a.custom6 || ''} ${a.custom7 || ''} ${a.custom8 || ''}</div>`
            ).join('');
        }

        function toggleAllAllocs(box) {
            document.querySelectorAll('.alloc-row-box').forEach(b => { b.checked = box.checked; });
        }

        function removeCheckedAllocations() {
            const keep = [];
            const rows = currentAllocations();
            const checked = new Set(
                Array.from(document.querySelectorAll('.alloc-row-box'))
                    .filter(b => b.checked).map(b => parseInt(b.dataset.i, 10)));
            rows.forEach((a, i) => { if (!checked.has(i)) keep.push(a); });
            const r = reportsData.find(x => x.name === selectedReportName);
            const t = r && (r.transactions || [])[allocIdx];
            if (t) t.allocations = keep;
            renderAllocationsList();
        }

        function showAddAllocation() {
            document.getElementById('add-allocation-form').style.display = 'block';
            ['custom6', 'custom7', 'custom8'].forEach(renderAllocField);
        }

        function renderAllocField(key) {
            const host = document.getElementById(`alloc-field-${key}`);
            if (!host) return;
            const label = {custom6: 'Department', custom7: 'Fund', custom8: 'Program'}[key];
            const val = allocDraft[key] || '';
            host.innerHTML = `
                <div data-nuiexp="field-${key}">
                    <span class="sapcnqr-form-field__label">${label}</span>
                    <div role="combobox" tabindex="0" aria-expanded="false"
                         data-nuiexp="field-${key}__trigger"
                         style="border:1px solid #c9c9c9; padding:4px; cursor:pointer;"
                         onclick="openAllocPicker('${key}')">${val}</div>
                </div>`;
        }

        function openAllocPicker(key) {
            allocActiveField = key;
            const popup = document.getElementById('alloc-popup');
            const search = document.getElementById('alloc-search');
            // The search box only exists once the picker is open, and production
            // looks it up by the field it belongs to.
            search.setAttribute('data-nuiexp', `field-${key}__input`);
            search.value = '';
            popup.style.display = 'block';
            const trig = document.querySelector(`[data-nuiexp='field-${key}__trigger']`);
            if (trig) trig.setAttribute('aria-expanded', 'true');
            renderAllocOptions('');
        }

        function renderAllocOptions(filter) {
            const box = document.getElementById('alloc-listbox');
            const q = (filter || '').toLowerCase();
            const opts = ALLOC_OPTIONS[allocActiveField] || [];
            box.innerHTML = opts
                .filter(([code]) => !q || code.toLowerCase().includes(q))
                .map(([code, desc]) => `<div role="option" aria-selected="false"
                        style="padding:4px; cursor:pointer;"
                        onclick="pickAllocOption('${code}')">${code}${desc}</div>`)
                .join('') || '<div class="no-match">No matches</div>';
        }

        function pickAllocOption(code) {
            allocDraft[allocActiveField] = code;
            const trig = document.querySelector(`[data-nuiexp='field-${allocActiveField}__trigger']`);
            if (trig) { trig.textContent = code; trig.setAttribute('aria-expanded', 'false'); }
            document.getElementById('alloc-popup').style.display = 'none';
            renderAllocField(allocActiveField);
        }

        function saveAddAllocation() {
            if (!allocDraft.custom6 && !allocDraft.custom7) return;
            const r = reportsData.find(x => x.name === selectedReportName);
            const t = r && (r.transactions || [])[allocIdx];
            if (t) {
                t.allocations = (t.allocations || []).concat([Object.assign({}, allocDraft)]);
            }
            document.getElementById('add-allocation-form').style.display = 'none';
            allocDraft = {};
            renderAllocationsList();
        }

        async function saveAllocations() {
            await fetch('/api/reports/allocate', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({report_name: selectedReportName, index: allocIdx,
                                      allocations: currentAllocations()})
            });
            const res = await fetch('/api/reports');
            reportsData = await res.json();
            closeAllocations();
        }

        // ---- Expense type field --------------------------------------------
        // Concur renders this as a role=combobox with NO input of its own; a
        // search box exists only while the picker is open, and each option's
        // label is the type name with its description appended. Modelling it as
        // a native <select> hid a production bug: the write path typed into the
        // field and pressed Enter, which against the real DOM meant typing into
        // a grid cell. A report named ...NATIVESELECT still renders a plain
        // <select> so that branch stays covered too.
        const EXPENSE_TYPES = [
            ["Ground Transportation", "Taxi, rideshare, rail and transit fares."],
            ["Office Supplies", "Consumables such as paper, pens and folders."],
            ["Lodging", "Hotel room charges and associated taxes."],
            ["Meal", "Meals taken while travelling on university business."],
            ["Software", "Application licenses, subscriptions and renewals."],
            ["Software Maintenance", "Support contracts renewed annually."]
        ];

        function renderExpenseTypeField(t, idx) {
            const current = t.expense_type || '';
            if ((selectedReportName || '').includes('NATIVESELECT')) {
                return `<label for="recon-type-${idx}">Expense Type</label>
                    <select class="recon-type" style="width:100%;" id="recon-type-${idx}"
                            data-nuiexp="field-expenseType">
                        <option value="">Expense Type...</option>
                        ${EXPENSE_TYPES.map(([n]) =>
                            `<option value="${n}" ${current === n ? 'selected' : ''}>${n}</option>`).join('')}
                    </select>`;
            }
            // No hidden input: real Concur has none, and an invisible element
            // whose id contains "type" is matched by the pane-readiness selector,
            // which then waits forever on something that can never be visible.
            return `
                <div data-nuiexp="field-expenseType" id="field-expenseType">
                    <div class="sapcnqr-form-field__heading"><span
                        class="sapcnqr-form-field__label">Expense Type</span><span
                        class="sapcnqr-form-field__required">*</span></div>
                    <div role="combobox" tabindex="0" aria-expanded="false"
                         data-nuiexp="field-expenseType__trigger"
                         style="border:1px solid #c9c9c9; padding:6px; cursor:pointer;"
                         onclick="openTypePicker(${idx})">${current}</div>
                </div>
                <!-- Help text sharing the word "type". The old wildcard selector
                     matched this and read it back as if it were the field. -->
                <div data-nuiexp="expense-type-quicktips" style="font-size:11px; color:#5c646b;">
                    InformationQuick TipsChoose the type that best describes the charge.Show Less
                </div>
                <div id="type-popup" class="sapcnqr-overlay__dialog" style="display:none;
                     border:1px solid #c9c9c9; background:white; padding:8px; z-index:5000;">
                    <input type="text" data-nuiexp="field-expenseType__input"
                           id="type-search" placeholder="Search for an expense type"
                           style="width:100%;" oninput="renderTypeOptions(${idx}, this.value)">
                    <div class="sapcnqr-selection-list__list-box" role="listbox"
                         id="type-listbox"></div>
                </div>`;
        }

        function openTypePicker(idx) {
            const popup = document.getElementById('type-popup');
            if (!popup) return;
            popup.style.display = 'block';
            const trig = document.querySelector("[data-nuiexp='field-expenseType__trigger']");
            if (trig) trig.setAttribute('aria-expanded', 'true');
            renderTypeOptions(idx, '');
        }

        function renderTypeOptions(idx, filter) {
            const box = document.getElementById('type-listbox');
            if (!box) return;
            const q = (filter || '').toLowerCase();
            const current = currentExpenseType(idx);
            box.innerHTML = EXPENSE_TYPES
                .filter(([n]) => !q || n.toLowerCase().includes(q))
                .map(([n, desc]) => `<div role="option" aria-selected="${n === current}"
                        style="padding:4px; cursor:pointer;"
                        onclick="pickExpenseType(${idx}, '${n.replace(/'/g, "\\\\'")}')"
                        >${n}${desc}</div>`)
                .join('') || '<div class="no-match">No matching expense types</div>';
        }

        function currentExpenseType(idx) {
            const sel = document.getElementById(`recon-type-${idx}`);
            if (sel && sel.tagName === 'SELECT') return sel.value || '';
            const trig = document.querySelector("[data-nuiexp='field-expenseType__trigger']");
            return trig ? (trig.textContent || '').trim() : '';
        }

        function pickExpenseType(idx, name) {
            const trig = document.querySelector("[data-nuiexp='field-expenseType__trigger']");
            if (trig) { trig.textContent = name; trig.setAttribute('aria-expanded', 'false'); }
            const popup = document.getElementById('type-popup');
            if (popup) popup.style.display = 'none';
        }

        // ---- Receipt panel -------------------------------------------------
        // Models live Concur: an async skeleton, a Receipt / Card Receipt toggle
        // that defaults to CARD on card transactions, and two mutually exclusive
        // states (drop zone vs viewer). Each of these hid a silent failure:
        //   * reading controls before hydration attaches nothing
        //   * uploading while the CARD tab is active is discarded
        //   * a viewer left standing makes the next expense look already-attached
        let receiptTab = 'CARD';

        function renderReceiptArea() {
            const t = selectedTx, idx = selectedTxIdx;
            const area = document.getElementById('receipt-area');
            if (!area) return;
            // Card transactions expose the toggle; cash ones do not.
            const hasTabs = t.card_transaction !== false;
            if (!hasTabs) receiptTab = 'RECEIPT';
            const tabs = hasTabs ? `
                <div class="receipt-tabs" data-nuiexp="receipt-tabs">
                  <ul role="listbox">
                    <li id="tab-RECEIPT" role="option" aria-selected="${receiptTab === 'RECEIPT'}"
                        onclick="switchReceiptTab('RECEIPT')"><div>Receipt</div></li>
                    <li id="tab-CARD" role="option" aria-selected="${receiptTab === 'CARD'}"
                        onclick="switchReceiptTab('CARD')"><div>Card Receipt</div></li>
                  </ul>
                </div>` : '';

            // The CARD tab carries its own input. Uploading there is silently
            // discarded by Concur, so the mock discards it too.
            const cardPane = `
                <div data-nuiexp="receipt-body">
                  <label for="upload-file" style="display:none;">Select a file for upload.
                    <input id="upload-file" type="file" data-nuiexp="erc-inp-upload-file"
                           onchange="onReceiptUpload(this, 'CARD')">
                  </label>
                  <div data-nuiexp="receipt-viewer-metadata"><span class="filename"></span></div>
                </div>`;

            const attached = `
                <div data-nuiexp="receipt-body">
                  <label for="upload-file" style="display:none;">Select a file for upload.
                    <input id="upload-file" type="file" data-nuiexp="erc-inp-upload-file"
                           onchange="onReceiptUpload(this, 'APPEND')">
                  </label>
                  <div data-nuiexp="receipt-viewer-metadata"><span class="filename">${t.receipt || ''}</span></div>
                  <div data-nuiexp="receipt-viewer-buttons" role="group" aria-label="Receipt Actions">
                    <button type="button" data-nuiexp="receipt-viewer__detach"
                            aria-label="Remove receipt: ${t.receipt || ''}"
                            onclick="removeReceipt(${idx})">Remove</button>
                    <button type="button" data-nuiexp="receipt-viewer__append" aria-label="Add Receipt">Add</button>
                  </div>
                </div>`;

            const empty = `
                <div class="spend-common__drag-n-drop" data-nuiexp="drag-drop-file">
                  <button id="upload-receipt-button" type="button">Upload New Receipt</button>
                  <input type="file" data-nuiexp="upload-receipt" aria-hidden="true" tabindex="-1"
                         style="display:none" onchange="onReceiptUpload(this, 'RECEIPT')">
                  <label for="upload-file" style="display:none;">Select a file for upload.
                    <input id="upload-file" type="file" data-nuiexp="upload-file"
                           onchange="onReceiptUpload(this, 'RECEIPT')">
                  </label>
                </div>`;

            const body = receiptTab === 'CARD' ? cardPane : (t.receipt ? attached : empty);
            area.innerHTML = tabs + body;
        }

        function hydrateReceiptArea() {
            const area = document.getElementById('receipt-area');
            if (!area) return;
            // Leave the previous content standing while "loading", which is what
            // let a stale viewer be read as the next expense's receipt.
            setTimeout(renderReceiptArea, 700);
        }

        function switchReceiptTab(which) {
            receiptTab = which;
            const area = document.getElementById('receipt-area');
            if (area) area.innerHTML = '<div class="entry-receipts-accessible-skeleton" aria-live="polite"></div>';
            setTimeout(renderReceiptArea, 500);
        }

        async function onReceiptUpload(input, mode) {
            const f = input.files && input.files[0];
            if (!f) return;
            // Uploading on the Card Receipt tab goes nowhere, exactly as in Concur.
            if (mode === 'CARD') { input.value = ''; return; }

            await fetch('/api/reports/attach_receipt', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ report_name: selectedReportName,
                                       index: selectedTxIdx, receipt_name: f.name })
            });
            const res = await fetch('/api/reports');
            reportsData = await res.json();
            const r = reportsData.find(x => x.name === selectedReportName);
            if (r) {
                selectedTx = (r.transactions || [])[selectedTxIdx];
                // Refresh the grid behind the pane, but leave the pane open: the
                // viewer that replaces the drop zone is what confirms the upload.
                showReportDetails(r);
            }
            receiptTab = 'RECEIPT';
            const area = document.getElementById('receipt-area');
            if (area) area.innerHTML = '<div class="entry-receipts-accessible-skeleton" aria-live="polite"></div>';
            setTimeout(renderReceiptArea, 600);
        }

        async function removeReceipt(idx) {
            await fetch('/api/reports/attach_receipt', {
                method: 'POST', headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ report_name: selectedReportName, index: idx, receipt_name: null })
            });
            const res = await fetch('/api/reports');
            reportsData = await res.json();
            const r = reportsData.find(x => x.name === selectedReportName);
            if (r) selectedTx = (r.transactions || [])[idx];
            const area = document.getElementById('receipt-area');
            if (area) area.innerHTML = '<div class="entry-receipts-accessible-skeleton" aria-live="polite"></div>';
            setTimeout(renderReceiptArea, 600);
        }

        function closeTransactionDetail() {
            document.getElementById('sapcnqr-layout-side-panel-elements').style.display = 'none';
        }

        // Concur's save dialogs. Both blocked a commit while ccworks reported
        // success: its detector looked for .sapMDialog / [role=dialog], and
        // Concur renders .sapcnqr-message-dialog with role="alertdialog".
        function showSaveDialog(html) {
            const d = document.createElement('div');
            d.className = 'sapcnqr-dialog sapcnqr-message-dialog';
            d.setAttribute('role', 'alertdialog');
            d.id = 'save-dialog';
            d.style.cssText = 'position:absolute; top:200px; left:200px; background:white;'
                + ' border:1px solid #c9c9c9; padding:20px; z-index:7000;';
            d.innerHTML = html;
            document.body.appendChild(d);
        }

        function closeSaveDialog() {
            const d = document.getElementById('save-dialog');
            if (d) d.remove();
        }

        async function saveReconTransaction(reportName, idx) {
            const expense_type = currentExpenseType(idx);
            const business_purpose = document.getElementById(`businessPurpose`).value;
            const comment = document.getElementById(`recon-comment`).value;
            const allocation_code = document.getElementById(`recon-allocation-${idx}`).value;

            // Expense type is required. A report named ...REQTYPE enforces it, so
            // the rest of the suite can keep saving rows that have no type.
            if (!expense_type && reportName.includes('REQTYPE')) {
                showSaveDialog('<div>Error</div><div>Before you can continue, you must '
                    + 'provide valid information for:</div><div>Expense Type</div>'
                    + '<button type="button" onclick="closeSaveDialog()">Close</button>');
                return;
            }

            // An expense that carries allocations asks whether to propagate the
            // change into them. Until answered the save does not commit -- which
            // is how an edit was silently discarded.
            const rep = reportsData.find(x => x.name === reportName);
            const tx = rep && (rep.transactions || [])[idx];
            const hasAllocs = tx && (tx.allocations || []).length > 0;
            const purposeChanged = tx && business_purpose !== (tx.business_purpose || '');
            if (hasAllocs && purposeChanged && !window.__updateAnswered) {
                showSaveDialog('<div>Update Other Items?</div><div>You changed the '
                    + 'following fields:</div><div>Business Purpose</div><div>Do you want '
                    + 'to also update your itemizations and allocations in this expense '
                    + 'with the same change?</div>'
                    + '<button type="button" onclick="answerUpdateOther(true)">Update</button>'
                    + '<button type="button" onclick="answerUpdateOther(false)">Do Not Update</button>'
                    + '<button type="button" onclick="closeSaveDialog()">Cancel</button>');
                window.__pendingSave = [reportName, idx];
                return;
            }
            window.__updateAnswered = false;
            
            await fetch('/api/reports/reconcile_transaction', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    report_name: reportName,
                    index: idx,
                    expense_type: expense_type,
                    business_purpose: business_purpose,
                    comment: comment,
                    allocation_code: allocation_code
                })
            });
            
            const res = await fetch('/api/reports');
            reportsData = await res.json();
            
            const updatedReport = reportsData.find(r => r.name === reportName);
            if (updatedReport) {
                showReportDetails(updatedReport);
            }
            delayRowMenus(1800);
            closeTransactionDetail();
        }

        async function uploadReceiptForTransaction(reportName, idx, fileName) {
            await fetch('/api/reports/attach_receipt', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    report_name: reportName,
                    index: idx,
                    receipt_name: fileName
                })
            });
            
            const res = await fetch('/api/reports');
            reportsData = await res.json();
            
            const updatedReport = reportsData.find(r => r.name === reportName);
            if (updatedReport) {
                showReportDetails(updatedReport);
            }
        }

        async function submitReport() {
            // Real Concur does not submit on this click. It opens a
            // report-totals confirmation as a full-screen overlay, and the
            // report stays in draft until the dialog's own button is clicked.
            // The toolbar button remains in the DOM behind the overlay and
            // still matches button:has-text('Submit Report'), which is what
            // made a page-wide .first locator resolve to the covered button
            // and time out.
            const reportName = selectedReportName;

            const overlay = document.createElement('div');
            overlay.id = 'report-totals-overlay';
            overlay.style.cssText = 'position:fixed; inset:0; background:rgba(0,0,0,0.35); z-index:9000; display:flex; align-items:center; justify-content:center;';

            const dialog = document.createElement('div');
            dialog.setAttribute('role', 'dialog');
            dialog.setAttribute('aria-modal', 'true');
            dialog.setAttribute('data-nuiexp', 'report-totals-modal');
            dialog.className = 'sapcnqr-dialog report-totals-modal sapcnqr-dialog__fade sapcnqr-dialog__fade--in';
            dialog.style.cssText = 'background:white; border-radius:6px; padding:25px; width:380px;';
            dialog.innerHTML = '<h3>Report Totals</h3><p>Amount Due Employee: <strong>$0.00</strong></p>';

            const confirmBtn = document.createElement('button');
            confirmBtn.className = 'button';
            confirmBtn.id = 'totals-modal-submit-btn';
            confirmBtn.setAttribute('data-nuiexp', 'reportTotalsModal.submitButton');
            confirmBtn.style.cssText = 'background:#0070d2; color:white; margin-right:10px;';
            confirmBtn.innerText = 'Submit Report';
            confirmBtn.onclick = async () => {
                await fetch('/api/reports/submit', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ report_name: reportName })
                });
                overlay.remove();
                closeReportDetails();
                fetchReports();
            };

            const cancelBtn = document.createElement('button');
            cancelBtn.className = 'button';
            cancelBtn.innerText = 'Cancel';
            cancelBtn.style.cssText = 'background:#e0e5ea;';
            cancelBtn.onclick = () => { overlay.remove(); };

            dialog.appendChild(confirmBtn);
            dialog.appendChild(cancelBtn);
            overlay.appendChild(dialog);
            document.body.appendChild(overlay);
        }

        function closeReportDetails() {
            document.getElementById('report-details-panel').style.display = 'none';
            document.getElementById('reports-container').style.display = 'block';
        }

        // Transaction Details
        function showTxModal(t) {
            document.getElementById('tx-merchant').innerText = t.merchant;
            document.getElementById('tx-date').innerText = t.date;
            document.getElementById('tx-amount').innerText = t.amount;
            document.getElementById('tx-id').innerText = t.id;
            document.getElementById('tx-program').innerText = t.program;
            document.getElementById('transaction-modal').style.display = 'block';
        }

        function closeTxModal() {
            document.getElementById('transaction-modal').style.display = 'none';
        }

        let selectedReceiptName = '';
        function showReceiptModal(name) {
            selectedReceiptName = name;
            document.getElementById('receipt-modal-name').innerText = name;
            document.getElementById('receipt-modal').style.display = 'block';
        }

        function closeReceiptModal() {
            document.getElementById('receipt-modal').style.display = 'none';
        }

        async function triggerDeleteReceipt() {
            if (confirm('Delete this receipt?')) {
                await fetch('/api/receipts/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: selectedReceiptName })
                });
                closeReceiptModal();
                fetchReceipts();
            }
        }

        function showEditModalForCurrentReport() {
            const r = reportsData.find(r => r.name === selectedReportName);
            if (r) {
                const idx = reportsData.indexOf(r);
                showEditModal(idx, r.name, r.purpose, r.comment);
            }
        }

        function showCreateModal() {
            document.getElementById('dialog-title').innerText = 'Create Report';
            document.getElementById('submit-report-btn').innerText = 'Create Report';
            document.getElementById('edit-index').value = '';
            document.getElementById('reportname').value = '';
            document.getElementById('purpose').value = '';
            document.getElementById('comment').value = '';
            document.getElementById('report-dialog').style.display = 'block';
        }

        function showEditModal(idx, name, purpose, comment) {
            document.getElementById('dialog-title').innerText = 'Edit Report';
            document.getElementById('submit-report-btn').innerText = 'Save';
            document.getElementById('edit-index').value = idx;
            document.getElementById('reportname').value = name;
            document.getElementById('purpose').value = purpose;
            document.getElementById('comment').value = comment;
            document.getElementById('report-dialog').style.display = 'block';
        }

        function closeDialog() {
            document.getElementById('report-dialog').style.display = 'none';
        }

        async function saveReport(e) {
            e.preventDefault();
            const idx = document.getElementById('edit-index').value;
            const report = {
                name: document.getElementById('reportname').value,
                purpose: document.getElementById('purpose').value,
                comment: document.getElementById('comment').value
            };

            let url = '/api/reports';
            let payload = report;
            
            if (idx !== '') {
                url = '/api/reports/update';
                payload = { index: parseInt(idx), ...report };
            }

            await fetch(url, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            closeDialog();
            fetchReports();
        }

        async function deleteReport(name) {
            if (confirm('Are you sure you want to delete this report?')) {
                await fetch('/api/reports/delete', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ name: name })
                });
                fetchReports();
            }
        }

        // Run fetches
        fetchReports();
        fetchReceipts();
        renderTransactions();
    </script>
</body>
</html>
"""

DELEGATES_HTML = """<!DOCTYPE html>
<html>
<head>
    <title>Expense Delegates</title>
    <style>
        body { font-family: sans-serif; padding: 20px; background-color: #f7f9fa; }
        .delegate-table { width: 100%; border-collapse: collapse; margin-top: 20px; background: white; }
        .delegate-table th, .delegate-table td { border: 1px solid #e0e5ea; padding: 10px; text-align: left; }
        .delegate-table th { background-color: #f0f3f6; }
        .button { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-weight: bold; }
        .btn-add { background-color: #0070d2; color: white; }
        .btn-delete { background-color: #c23934; color: white; }
        .btn-save { background-color: #04844b; color: white; margin-top: 15px; }
        #search-container { display: none; margin-top: 15px; background: white; padding: 15px; border: 1px solid #c9c9c9; border-radius: 4px; }
        .suggestion-item { padding: 8px; cursor: pointer; }
        .suggestion-item:hover { background-color: #e0e5ea; }
    </style>
</head>
<body>
    <h1>Expense Delegates</h1>
    <div>
        <button class="button btn-add" id="add-delegate-btn" onclick="showSearch()">Add</button>
        <button class="button btn-delete" id="delete-delegate-btn" onclick="deleteSelected()">Delete</button>
    </div>
    
    <div id="search-container">
        <h3>Search for Delegate</h3>
        <input type="text" id="delegate-search-input" placeholder="Type name or email..." oninput="showSuggestions(this.value)">
        <div id="suggestions" style="border: 1px solid #ccc; max-height: 100px; overflow-y: auto; display:none;">
            <div class="suggestion-item" id="suggestion-john" onclick="selectDelegate('John Doe', 'john@example.com')">John Doe (john@example.com)</div>
            <div class="suggestion-item" id="suggestion-jane" onclick="selectDelegate('Jane Smith', 'jane@example.com')">Jane Smith (jane@example.com)</div>
        </div>
    </div>

    <table class="delegate-table" id="delegates-table">
        <thead>
            <tr>
                <th>Select</th>
                <th>Delegate Name</th>
                <th>Can Prepare</th>
                <th>Can Submit Reports</th>
                <th>Can Approve</th>
            </tr>
        </thead>
        <tbody id="delegates-body">
            <!-- Statically initialised or dynamic delegates -->
        </tbody>
    </table>
    
    <button class="button btn-save" id="save-delegates-btn" onclick="saveDelegates()">Save</button>

    <script>
        let delegates = [];

        async function fetchDelegates() {
            const res = await fetch('/api/delegates');
            delegates = await res.json();
            renderDelegates();
        }

        function renderDelegates() {
            const tbody = document.getElementById('delegates-body');
            tbody.innerHTML = '';
            delegates.forEach((d, idx) => {
                const tr = document.createElement('tr');
                tr.className = 'delegate-row';
                tr.innerHTML = `
                    <td><input type="checkbox" class="delegate-select-chk" data-idx="${idx}"></td>
                    <td class="delegate-name-cell">${d.name} (${d.email})</td>
                    <td><input type="checkbox" class="perm-prepare" ${d.prepare ? 'checked' : ''}></td>
                    <td><input type="checkbox" class="perm-submit" ${d.submit ? 'checked' : ''}></td>
                    <td><input type="checkbox" class="perm-approve" ${d.approve ? 'checked' : ''}></td>
                `;
                tbody.appendChild(tr);
            });
        }

        function showSearch() {
            document.getElementById('search-container').style.display = 'block';
        }

        function showSuggestions(val) {
            const sug = document.getElementById('suggestions');
            if (val.length > 1) {
                sug.style.display = 'block';
            } else {
                sug.style.display = 'none';
            }
        }

        function selectDelegate(name, email) {
            delegates.push({ name: name, email: email, prepare: false, submit: false, approve: false });
            document.getElementById('search-container').style.display = 'none';
            document.getElementById('delegate-search-input').value = '';
            document.getElementById('suggestions').style.display = 'none';
            renderDelegates();
        }

        function deleteSelected() {
            const chks = document.querySelectorAll('.delegate-select-chk');
            let toRemove = [];
            chks.forEach(chk => {
                if (chk.checked) {
                    toRemove.push(parseInt(chk.getAttribute('data-idx')));
                }
            });
            delegates = delegates.filter((d, idx) => !toRemove.includes(idx));
            renderDelegates();
        }

        async function saveDelegates() {
            const rows = document.querySelectorAll('#delegates-body tr');
            rows.forEach((row, idx) => {
                delegates[idx].prepare = row.querySelector('.perm-prepare').checked;
                delegates[idx].submit = row.querySelector('.perm-submit').checked;
                delegates[idx].approve = row.querySelector('.perm-approve').checked;
            });
            
            await fetch('/api/delegates/save', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ delegates: delegates })
            });
            alert('Saved successfully!');
        }

        fetchDelegates();
    </script>
</body>
</html>
"""

LOGIN_HTML = """<!DOCTYPE html>
<html>
<head><title>SAP Concur Login</title></head>
<body>
  <h1>Mock SAP Concur Login</h1>
  <form action="/login-submit" method="GET">
    <input type="text" id="username" placeholder="Username"><br><br>
    <input type="password" id="password" placeholder="Password"><br><br>
    <button type="submit" id="login-btn">Sign In</button>
  </form>
</body>
</html>
"""


class MockConcurRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/" or "login" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(LOGIN_HTML.encode("utf-8"))
        elif self.path == "/nui/expense":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_HTML.encode("utf-8"))
        elif "profile/editdelegates.asp" in self.path:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DELEGATES_HTML.encode("utf-8"))
        elif self.path == "/api/reports":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(REPORTS).encode("utf-8"))
        elif self.path == "/api/receipts":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(RECEIPTS).encode("utf-8"))
        elif self.path == "/api/delegates":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(DELEGATES).encode("utf-8"))
        elif "/login-submit" in self.path:
            self.send_response(302)
            self.send_header("Location", "/nui/expense")
            self.send_header("Set-Cookie", "concur_mock_session=active_state; Path=/; HttpOnly")
            self.end_headers()
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get('Content-Length', 0))
        post_data = self.rfile.read(content_length)
        
        try:
            data = json.loads(post_data.decode('utf-8')) if post_data else {}
        except Exception:
            data = {}

        if self.path == "/api/reports":
            name = data.get("name", "Unnamed")
            purpose = data.get("purpose", "")
            comment = data.get("comment", "")
            # A report whose name contains DUPES is seeded with two byte-identical
            # line items, reproducing the ordinary purchasing-card case of two
            # shipments booked the same day for the same amount. ccworks used to
            # deduplicate rows by their text content and silently drop the second.
            # Keyed off the name so existing tests' row counts are unaffected.
            if "DUPES" in name:
                transactions = [
                    {"id": "TX_DUP_1", "merchant": "ESHIPGLOBAL INC", "amount": "$53.77", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False},
                    {"id": "TX_DUP_2", "merchant": "ESHIPGLOBAL INC", "amount": "$53.77", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False},
                    {"id": "TX_DUP_3", "merchant": "Office Depot", "amount": "$189.99", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False},
                ]
            # A report whose name contains RECEIPTS is seeded for the receipt
            # paths: three rows sharing a merchant AND amount (so only the index
            # can distinguish them), one row that already holds a receipt (the
            # replace path), and one non-card row that has no Receipt/Card
            # Receipt toggle.
            elif "RECEIPTS" in name:
                transactions = [
                    {"id": "TX_RC_1", "merchant": "ESHIPGLOBAL INC", "amount": "$23.28", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False},
                    {"id": "TX_RC_2", "merchant": "ESHIPGLOBAL INC", "amount": "$23.28", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False},
                    {"id": "TX_RC_3", "merchant": "ESHIPGLOBAL INC", "amount": "$23.28", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False},
                    {"id": "TX_RC_4", "merchant": "ANTHROPIC", "amount": "$400.00", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False, "receipt": "old.pdf"},
                    {"id": "TX_RC_5", "merchant": "PETTY CASH", "amount": "$12.00", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False, "card_transaction": False},
                ]
            else:
                transactions = [
                    {"id": "TX_REP_1", "merchant": "Uber", "amount": "$24.50", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False},
                    {"id": "TX_REP_2", "merchant": "Office Depot", "amount": "$189.99", "expense_type": "", "business_purpose": "", "comment": "", "allocation_code": "", "reconciled": False}
                ]
            REPORTS.append({
                "name": name,
                "purpose": purpose,
                "comment": comment,
                "status": "Draft",
                "transactions": transactions,
            })
            
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))

        elif self.path == "/api/reports/reconcile_transaction":
            report_name = data.get("report_name")
            tx_idx = data.get("index")
            for r in REPORTS:
                if r["name"] == report_name:
                    txs = r.get("transactions", [])
                    if 0 <= tx_idx < len(txs):
                        txs[tx_idx]["expense_type"] = data.get("expense_type", "")
                        txs[tx_idx]["business_purpose"] = data.get("business_purpose", "")
                        txs[tx_idx]["comment"] = data.get("comment", "")
                        txs[tx_idx]["allocation_code"] = data.get("allocation_code", "")
                        txs[tx_idx]["reconciled"] = True
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))

        elif self.path == "/api/reports/allocate":
            report_name = data.get("report_name")
            tx_idx = data.get("index")
            allocations = data.get("allocations") or []
            for r in REPORTS:
                if r["name"] == report_name:
                    txs = r.get("transactions", [])
                    if 0 <= tx_idx < len(txs):
                        txs[tx_idx]["allocations"] = allocations

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))

        elif self.path == "/api/reports/attach_receipt":
            report_name = data.get("report_name")
            tx_idx = data.get("index")
            receipt_name = data.get("receipt_name")
            for r in REPORTS:
                if r["name"] == report_name:
                    txs = r.get("transactions", [])
                    if 0 <= tx_idx < len(txs):
                        # A falsy name detaches. Storing None instead would leave a
                        # viewer with an empty filename, which is exactly the state
                        # that made a removed receipt look like an attached one.
                        if receipt_name:
                            txs[tx_idx]["receipt"] = receipt_name
                        else:
                            txs[tx_idx].pop("receipt", None)

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))

        elif self.path == "/api/reports/submit":
            report_name = data.get("report_name")
            for r in REPORTS:
                if r["name"] == report_name:
                    r["status"] = "Submitted"
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))

        elif self.path == "/api/reports/update":
            idx = data.get("index")
            if idx is not None and 0 <= idx < len(REPORTS):
                REPORTS[idx]["name"] = data.get("name", REPORTS[idx]["name"])
                REPORTS[idx]["purpose"] = data.get("purpose", REPORTS[idx]["purpose"])
                REPORTS[idx]["comment"] = data.get("comment", REPORTS[idx]["comment"])
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))

        elif self.path == "/api/reports/delete":
            name = data.get("name")
            REPORTS[:] = [r for r in REPORTS if r["name"] != name]
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))

        elif self.path == "/api/receipts/delete":
            name = data.get("name")
            RECEIPTS[:] = [r for r in RECEIPTS if r["name"] != name]
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
            
        elif self.path == "/api/delegates/save":
            new_delegates = data.get("delegates", [])
            DELEGATES.clear()
            DELEGATES.extend(new_delegates)
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"success": True}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


class MockConcurServer:
    def __init__(self, host="127.0.0.1", port=8090):
        self.host = host
        self.port = port
        self.httpd = None
        self.thread = None

    def start(self):
        # Reset states
        REPORTS.clear()
        RECEIPTS[:] = [
            {"name": "lunch_receipt.png"},
            {"name": "taxi_receipt.png"},
            {"name": "hotel_receipt.jpg"}
        ]
        DELEGATES.clear()
        DELEGATES.append(
            {"name": "Existing Delegate", "email": "existing@example.com", "prepare": True, "submit": False, "approve": False}
        )
        
        self.httpd = HTTPServer((self.host, self.port), MockConcurRequestHandler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        print(f"Mock SAP Concur server running at http://{self.host}:{self.port}")

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            print("Mock SAP Concur server stopped.")
