import { expect, request, test, type APIRequestContext, type Page } from "@playwright/test";
import { Buffer } from "node:buffer";

const apiBase = process.env.E2E_API_URL ?? "http://127.0.0.1:8000/api/v1";
const password = "E2E-only-password-2026!";

async function apiFor(token: string): Promise<APIRequestContext> {
  return request.newContext({ baseURL: `${apiBase}/`, extraHTTPHeaders: { Authorization: `Bearer ${token}` } });
}

async function register(api: APIRequestContext, label: string) {
  const response = await api.post("auth/register", {
    data: { email: `${label}-${Date.now()}@example.com`, full_name: `E2E ${label}`, password },
  });
  expect(response.status()).toBe(201);
  return response.json() as Promise<{ access_token: string; user: { id: string; email: string } }>;
}

async function currentToken(page: Page) {
  const token = await page.evaluate(() => localStorage.getItem("incident_token"));
  expect(token).toBeTruthy();
  return token as string;
}

test("browser verifies the isolated Phase 1 lifecycle end to end", async ({ page }, testInfo) => {
  test.setTimeout(240_000);
  const pageErrors: string[] = [];
  const serverErrors: string[] = [];
  const analyticsChunkResponses: number[] = [];
  const roleApis: APIRequestContext[] = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("response", response => {
    if (response.status() >= 500) serverErrors.push(`${response.status()} ${response.url()}`);
    if (/AnalyticsPage(?:-[^/]+)?\.(?:js|tsx)/.test(response.url())) analyticsChunkResponses.push(response.status());
  });

  await page.goto("/");
  await page.getByRole("button", { name: "Create account" }).click();
  const email = `phase1-${Date.now()}@example.com`;
  await page.locator('[name="full_name"]').fill("Phase 1 E2E Admin");
  await page.locator('[name="email"]').fill(email);
  await page.locator('[name="password"]').fill(password);
  await page.getByRole("button", { name: /Create account/ }).last().click();
  await page.locator('[name="workspace"]').fill("Phase 1 E2E Workspace");
  await page.locator('[name="project"]').fill("E2E Payments Project");
  await page.getByRole("button", { name: /Create workspace/ }).click();
  await expect(page.getByRole("heading", { name: "E2E Payments Project" })).toBeVisible();

  const adminApi = await apiFor(await currentToken(page));
  const workspace = (await (await adminApi.get("workspaces")).json())[0] as { id: string };
  const project = (await (await adminApi.get("projects")).json())[0] as { id: string };
  const adminUser = await (await adminApi.get("auth/me")).json() as { id: string };

  // Add a workspace administrator, project roles, and a completely separate tenant.
  const roles: Record<string, { access_token: string; user: { id: string; email: string } }> = {};
  for (const role of ["workspace-admin", "incident_manager", "engineer", "viewer"]) {
    roles[role] = await register(adminApi, role);
    const workspaceInvite = await adminApi.post(`workspaces/${workspace.id}/members`, {
      data: { email: roles[role].user.email, role: role === "workspace-admin" ? "admin" : "member" },
    });
    expect(workspaceInvite.status()).toBe(201);
    if (role !== "workspace-admin") {
      const projectInvite = await adminApi.post(`projects/${project.id}/members`, {
        data: { email: roles[role].user.email, role },
      });
      expect(projectInvite.status()).toBe(201);
    }
  }
  const tenantBUser = await register(adminApi, "tenant-b");
  const tenantBApi = await apiFor(tenantBUser.access_token);
  const workspaceBResponse = await tenantBApi.post("workspaces", { data: { name: "Isolated Workspace B" } });
  expect(workspaceBResponse.status()).toBe(201);
  const workspaceB = await workspaceBResponse.json() as { id: string };
  const projectBResponse = await tenantBApi.post("projects", {
    data: { workspace_id: workspaceB.id, name: "Isolated Project B" },
  });
  expect(projectBResponse.status()).toBe(201);
  const projectB = await projectBResponse.json() as { id: string };
  const orgWorkspaceResponse = await adminApi.post("workspaces", { data: { name: "Org Admin Boundary" } });
  expect(orgWorkspaceResponse.status()).toBe(201);
  const orgWorkspace = await orgWorkspaceResponse.json() as { id: string };
  expect((await adminApi.post(`workspaces/${orgWorkspace.id}/members`, {
    data: { email: roles["workspace-admin"].user.email, role: "member" },
  })).status()).toBe(201);
  const workspaceAdmin = await apiFor(roles["workspace-admin"].access_token);
  roleApis.push(workspaceAdmin);
  const projectCResponse = await workspaceAdmin.post("projects", {
    data: { workspace_id: orgWorkspace.id, name: "Org Admin Isolated Project" },
  });
  expect(projectCResponse.status()).toBe(201);
  const projectC = await projectCResponse.json() as { id: string };
  const tenantBProjects = await (await tenantBApi.get("projects")).json() as Array<{ id: string }>;
  expect(tenantBProjects.map(row => row.id)).toEqual([projectB.id]);
  expect((await tenantBApi.get(`workspaces/${workspace.id}/members`)).status()).toBe(403);
  expect((await tenantBApi.get(`projects/${project.id}/services`)).status()).toBe(403);
  expect((await tenantBApi.get(`projects/${project.id}/incidents`)).status()).toBe(403);
  expect((await adminApi.get(`projects/${projectB.id}/services`)).status()).toBe(403);
  const adminProjects = await (await adminApi.get("projects")).json() as Array<{ id: string }>;
  expect(adminProjects.some(row => row.id === projectB.id)).toBeFalsy();
  expect(adminProjects.some(row => row.id === projectC.id)).toBeFalsy();
  expect((await adminApi.get(`projects/${projectC.id}/services`)).status()).toBe(403);
  expect((await adminApi.post(`workspaces/${orgWorkspace.id}/teams`, {
    data: { name: "Authorized Org Team" },
  })).status()).toBe(201);
  const orgAdmin = await apiFor(roles["workspace-admin"].access_token);
  roleApis.push(orgAdmin);
  expect((await orgAdmin.get(`projects/${project.id}/services`)).status()).toBe(403);
  expect((await orgAdmin.get(`workspaces/${workspace.id}/members`)).status()).toBe(200);

  // Role matrix: readers can inspect; only write-capable project roles can mutate.
  for (const role of ["incident_manager", "engineer", "viewer"]) {
    const roleApi = await apiFor(roles[role].access_token);
    roleApis.push(roleApi);
    expect((await roleApi.get(`projects/${project.id}/services`)).status()).toBe(200);
    expect((await roleApi.get(`projects/${project.id}/incidents`)).status()).toBe(200);
    expect((await roleApi.get(`projects/${project.id}/integrations`)).status()).toBe(200);
    const writeService = await roleApi.post(`projects/${project.id}/services`, {
      data: { name: `role-check-${role}` },
    });
    expect(writeService.status()).toBe(role === "viewer" ? 403 : 201);
    const writeIncident = await roleApi.post(`projects/${project.id}/incidents`, {
      data: { title: `role incident ${role}`, severity: "SEV-3" },
    });
    expect(writeIncident.status()).toBe(role === "viewer" ? 403 : 201);
  }

  // Service catalog UI, then API update and dependency, verified back in the browser.
  await page.getByRole("link", { name: "Services" }).click();
  await page.getByRole("button", { name: /Add service/ }).click();
  await page.locator('input[name="name"]').fill("checkout-api");
  await page.locator('input[name="description"]').fill("Initial synthetic service");
  await page.getByRole("button", { name: "Save service" }).click();
  await expect(page.getByRole("link", { name: /checkout-api/ })).toBeVisible();
  await page.getByRole("button", { name: /Add service/ }).click();
  await page.locator('input[name="name"]').fill("payments-db");
  await page.getByRole("button", { name: "Save service" }).click();
  const checkoutHref = await page.getByRole("link", { name: /checkout-api/ }).getAttribute("href");
  const databaseHref = await page.getByRole("link", { name: /payments-db/ }).getAttribute("href");
  const checkoutId = checkoutHref!.split("/").pop();
  const databaseId = databaseHref!.split("/").pop();
  expect((await adminApi.patch(`services/${checkoutId}`, { data: { description: "Updated synthetic service details" } })).status()).toBe(200);
  expect((await adminApi.post(`services/${checkoutId}/dependencies`, {
    data: { target_service_id: databaseId, relationship_type: "calls" },
  })).status()).toBe(201);
  await page.goto(`/services/${checkoutId}`);
  await expect(page.getByText("Updated synthetic service details")).toBeVisible();
  await expect(page.getByRole("link", { name: "payments-db" })).toBeVisible();

  // Create and update an issue; verify comment and activity history.
  await page.getByRole("link", { name: "Issues" }).click();
  await page.getByRole("button", { name: /New issue/ }).click();
  await page.locator('input[name="title"]').fill("Synthetic checkout follow-up");
  await page.locator('textarea[name="description"]').fill("Synthetic test-only issue");
  await page.getByRole("button", { name: "Create issue" }).click();
  await expect(page.getByText("Synthetic checkout follow-up")).toBeVisible();
  const issues = await (await adminApi.get(`projects/${project.id}/issues`)).json() as Array<{ id: string; title: string }>;
  const issue = issues.find(row => row.title === "Synthetic checkout follow-up")!;
  await page.goto(`/issues/${issue.id}`);
  await page.getByLabel("Issue status").selectOption("In Progress");
  await expect.poll(async () => (await adminApi.get(`issues/${issue.id}`)).json().then(row => row.status)).toBe("In Progress");
  await page.getByPlaceholder(/Add a comment/).fill("Synthetic issue comment");
  await page.getByRole("button", { name: "Post" }).click();
  await expect(page.getByText("Synthetic issue comment", { exact: true })).toBeVisible();
  await expect(page.getByText("status.changed")).toBeVisible();

  // Incident assignment, status, comment, upload, and same-project issue linking.
  await page.getByRole("link", { name: "Incidents" }).click();
  await page.getByRole("button", { name: "New incident" }).click();
  await page.locator('input[name="title"]').fill("Synthetic checkout incident");
  await page.locator('textarea[name="description"]').fill("Harmless E2E incident with synthetic diagnostics");
  await page.getByRole("button", { name: "Create incident" }).click();
  await page.getByRole("link", { name: /Synthetic checkout incident/ }).click();
  const incidents = await (await adminApi.get(`projects/${project.id}/incidents`)).json() as Array<{ id: string; title: string }>;
  const incident = incidents.find(row => row.title === "Synthetic checkout incident")!;
  page.once("dialog", dialog => dialog.accept(adminUser.id));
  await page.getByRole("button", { name: "Assign" }).click();
  await expect.poll(async () => (await adminApi.get(`incidents/${incident.id}`)).json().then(row => row.assignee_id)).toBe(adminUser.id);
  await page.getByRole("button", { name: "Acknowledge" }).click();
  await expect.poll(async () => (await adminApi.get(`incidents/${incident.id}`)).json().then(row => row.status)).toBe("acknowledged");
  await page.getByPlaceholder("Paste log excerpt or diagnostic observation…").fill("Synthetic evidence note from the incident UI");
  await page.getByRole("button", { name: "Add note" }).click();
  await expect(page.getByText("Synthetic evidence note from the incident UI")).toBeVisible();
  await page.getByPlaceholder("Add an incident comment").fill("Synthetic incident timeline note");
  await page.getByRole("button", { name: "Comment" }).click();
  await expect(page.getByText("Synthetic incident timeline note")).toBeVisible();
  await page.getByLabel("Evidence file").setInputFiles({
    name: "synthetic-diagnostic.log", mimeType: "text/plain",
    buffer: Buffer.from("2026-01-01T00:00:00Z ERROR synthetic checkout timeout; no real system data."),
  });
  await page.getByRole("button", { name: "Upload file" }).click();
  await expect(page.getByText(/ERROR synthetic checkout timeout/)).toBeVisible();
  expect((await adminApi.post(`incidents/${incident.id}/issues/${issue.id}`)).status()).toBe(201);
  const incidentData = await (await adminApi.get(`incidents/${incident.id}`)).json();
  expect(incidentData.issues.some((row: { id: string }) => row.id === issue.id)).toBeTruthy();

  // Configure an adapter placeholder, replace a synthetic credential, and test status honesty.
  await page.getByRole("link", { name: "Integrations" }).click();
  await page.getByLabel("Integration configuration").fill('{"base_url":"http://127.0.0.1:9"}');
  const configureResponsePromise = page.waitForResponse(response =>
    response.url().includes(`/projects/${project.id}/integrations`) && response.request().method() === "POST");
  await page.getByRole("button", { name: "Configure" }).click();
  expect((await configureResponsePromise).status()).toBe(201);
  await expect(page.getByText("Adapter not implemented").first()).toBeVisible();
  const integrationRows = await (await adminApi.get(`projects/${project.id}/integrations`)).json() as Array<{ id: string; provider: string }>;
  const integration = integrationRows.find(row => row.provider === "prometheus");
  expect(integration).toBeTruthy();
  const syntheticCredential = "SYNTHETIC-E2E-CREDENTIAL-DO-NOT-USE";
  await page.getByLabel("Replacement integration credential").fill(syntheticCredential);
  await page.getByRole("button", { name: "Save secret" }).click();
  await expect(page.getByText("Credentials: ********")).toBeVisible();
  const integrations = await (await adminApi.get(`projects/${project.id}/integrations`)).json();
  expect(integrations[0].credentials).toBe("********");
  expect(JSON.stringify(integrations)).not.toContain(syntheticCredential);
  expect(JSON.stringify(integrations)).not.toContain("credentials_ciphertext");
  for (const role of ["workspace-admin", "incident_manager", "engineer", "viewer"]) {
    const roleApi = await apiFor(roles[role].access_token);
    roleApis.push(roleApi);
    expect((await roleApi.put(`integrations/${integration.id}/credentials`, {
      data: { credentials: { token: "SYNTHETIC-UNAUTHORIZED-TEST-VALUE" } },
    })).status()).toBe(403);
  }
  const connectorTest = await adminApi.post(`integrations/${integration.id}/test`);
  expect(connectorTest.status()).toBe(200);
  expect((await connectorTest.json()).connection_test).toBe("INVALID_CONFIGURATION");
  const testedIntegration = await (await adminApi.get(`projects/${project.id}/integrations`)).json();
  expect(testedIntegration[0].health_status).toBe("degraded");

  // Queue a real Celery investigation in the isolated stack and verify run-scoped citations.
  await page.getByRole("link", { name: "Incidents" }).click();
  await page.getByRole("link", { name: /Synthetic checkout incident/ }).click();
  const startResponsePromise = page.waitForResponse(response =>
    response.url().includes(`/incidents/${incident.id}/investigations`) && response.request().method() === "POST");
  await page.getByRole("button", { name: "Investigate" }).click();
  const startResponse = await startResponsePromise;
  expect(startResponse.status()).toBe(202);
  const createdRun = await startResponse.json() as { id: string };
  await expect.poll(async () => {
    const response = await adminApi.get(`investigations/${createdRun.id}`);
    if (!response.ok()) return "request-failed";
    return (await response.json()).status;
  }, { timeout: 120_000, intervals: [1_000, 2_000, 3_000] }).toBe("waiting_for_review");
  const run = await (await adminApi.get(`investigations/${createdRun.id}`)).json();
  expect(run.execution_attempts.some((attempt: { status: string }) => attempt.status === "succeeded")).toBeTruthy();
  expect(run.findings.length).toBeGreaterThan(0);
  const runEvidence = new Set(run.evidence.map((item: { id: string }) => item.id));
  for (const finding of run.findings) {
    for (const citation of finding.evidence) expect(runEvidence.has(citation.id)).toBeTruthy();
  }
  expect(run.source_statuses.some((source: { source_type: string; status: string }) => source.source_type === "metrics" && source.status === "unavailable")).toBeTruthy();
  expect(run.source_statuses.some((source: { source_type: string; status: string }) => source.source_type === "traces" && source.status === "unavailable")).toBeTruthy();
  await page.goto(`/investigations/${createdRun.id}`);
  await expect(page.getByRole("heading", { name: "Root-cause analysis" })).toBeVisible();
  await expect(page.getByText("Evidence associated with this run")).toBeVisible();
  const syntheticReviewNote = "SYNTHETIC-REVIEW-NOTE-SECRET";
  await page.route("**/findings/*/review", async route => {
    const submitted = JSON.parse(route.request().postData() ?? "{}");
    await route.continue({ postData: JSON.stringify({ ...submitted, note: syntheticReviewNote }) });
  });
  await page.getByRole("button", { name: "Need evidence" }).first().click();
  await page.unroute("**/findings/*/review");
  await expect(page.getByRole("status")).toContainText("needs_more_evidence");
  await expect.poll(async () => (await adminApi.get(`investigations/${createdRun.id}`)).json()
    .then(row => row.findings[0].review_status)).toBe("needs_more_evidence");

  // Exercise role boundaries against the resulting recommendation and approval.
  const recommendation = run.recommendations[0] as { id: string };
  const approvalDenied = await apiFor(roles.viewer.access_token);
  roleApis.push(approvalDenied);
  expect((await approvalDenied.post(`recommendations/${recommendation.id}/approvals`)).status()).toBe(403);
  const approvalResponse = await adminApi.post(`recommendations/${recommendation.id}/approvals`);
  expect(approvalResponse.status()).toBe(201);
  const approval = await approvalResponse.json() as { id: string };
  for (const role of ["workspace-admin", "incident_manager", "engineer", "viewer"]) {
    const roleApi = await apiFor(roles[role].access_token);
    roleApis.push(roleApi);
    expect((await roleApi.post(`approvals/${approval.id}/decision`, {
      data: { status: "approved", note: "Synthetic unauthorized decision" },
    })).status()).toBe(403);
  }
  await page.getByRole("link", { name: "Approvals" }).click();
  await expect(page.getByRole("heading", { name: "Approval queue", exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Approve" }).click();
  await expect(page.getByRole("status")).toContainText("approved");
  await page.getByRole("button", { name: "Run simulation" }).click();
  await expect(page.getByRole("status")).toContainText("No production systems were changed.");

  // Analytics must fetch a distinct lazy chunk; audit UI must show actions and timestamps.
  await page.getByRole("link", { name: "Analytics" }).click();
  await expect(page.getByRole("heading", { name: "Analytics" })).toBeVisible();
  await expect.poll(() => analyticsChunkResponses.length).toBeGreaterThan(0);
  expect(analyticsChunkResponses.every(status => status === 200)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath("phase1-analytics.png") });
  await page.getByRole("link", { name: "Audit trail" }).click();
  await expect(page.getByText("approval.approved")).toBeVisible();
  await expect(page.locator("time").first()).toBeVisible();
  const events = await (await adminApi.get("audit", { params: { project_id: project.id } })).json();
  expect(events.some((event: { action: string }) => event.action === "approval.requested")).toBeTruthy();
  expect(events.some((event: { action: string }) => event.action === "remediation.simulated")).toBeTruthy();
  expect(JSON.stringify(events)).not.toContain(syntheticCredential);
  expect(JSON.stringify(events)).not.toContain("Synthetic unauthorized decision");
  expect(JSON.stringify(events)).not.toContain(syntheticReviewNote);

  // Logout and login are browser-verified. Then check another tenant's direct route is rejected.
  await page.setViewportSize({ width: 1280, height: 720 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.locator('button[aria-label="Sign out"]').evaluate(button => (button as HTMLButtonElement).click());
  await expect(page.getByRole("heading", { name: "Sign in to IncidentIQ" })).toBeVisible();
  await page.locator('[name="email"]').fill(tenantBUser.user.email);
  await page.locator('[name="password"]').fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByLabel("Project")).toHaveValue(projectB.id);
  const forbiddenRequest = page.waitForResponse(response =>
    response.url().includes(`/api/v1/incidents/${incident.id}`) && response.request().method() === "GET");
  await page.goto(`/incidents/${incident.id}`);
  expect((await forbiddenRequest).status()).toBe(403);
  await expect(page.getByRole("status")).toContainText("Explicit project membership is required");

  expect(pageErrors).toEqual([]);
  expect(serverErrors).toEqual([]);
  await adminApi.dispose();
  await tenantBApi.dispose();
  for (const context of roleApis) await context.dispose();
});
