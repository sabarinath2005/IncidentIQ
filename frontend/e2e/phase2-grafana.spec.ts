import { expect, test } from "@playwright/test";

test("Grafana observability dashboard renders populated panels", async ({ page }, testInfo) => {
  test.setTimeout(60_000);
  const baseUrl = process.env.GRAFANA_BASE_URL;
  expect(baseUrl, "GRAFANA_BASE_URL must target the isolated Phase 2 stack").toBeTruthy();

  await page.goto(`${baseUrl}/d/incidentiq-payment-api/incidentiq-payment-api?orgId=1&refresh=10s`, {
    waitUntil: "domcontentloaded",
  });
  await expect(page.getByText("IncidentIQ Payment API", { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1_000);
  await page.screenshot({ path: testInfo.outputPath("grafana-dashboard-loaded.png"), fullPage: true });

  for (const title of [
    "Payment requests per second",
    "Payment error ratio",
    "Payment API p95 latency",
    "SQLite connection pool utilization",
    "Payment API logs",
  ]) {
    await expect(page.getByText(title, { exact: true })).toBeVisible();
  }
  for (const title of [
    "Payment requests per second",
    "Payment error ratio",
    "Payment API p95 latency",
    "SQLite connection pool utilization",
  ]) {
    const panel = page.getByRole("region", { name: title });
    await expect(panel.getByText("No data", { exact: true })).toHaveCount(0, { timeout: 30_000 });
    await expect.poll(() => panel.locator("canvas, svg path").count(), { timeout: 30_000 }).toBeGreaterThan(0);
  }
  await expect(page.getByText("controlled payment failure").first()).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath("grafana-payment-api.png"), fullPage: true });
});
