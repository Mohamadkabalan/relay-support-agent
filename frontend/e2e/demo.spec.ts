import { expect, type Page, test } from "@playwright/test";

async function noSidewaysScroll(page: Page) {
  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow).toBeLessThanOrEqual(0);
}

test("a refund over $100 waits for an operator, and the customer hears the decision", async ({
  page,
  context,
}) => {
  await page.goto("/");
  await expect(page.getByText("Demo data. Don't enter real personal information.")).toBeVisible();
  await page.getByRole("button", { name: "Be Dana" }).click();
  await expect(page.getByRole("button", { name: "Signed in as Dana" })).toBeVisible();
  await noSidewaysScroll(page);

  await page.getByRole("button", { name: "Chat with support" }).click();
  const chat = page.getByRole("region", { name: "Support chat" });
  const input = chat.getByLabel("Your message");
  await input.fill("My headphones arrived with a cracked headband. Can I get a refund?");
  await input.press("Enter");
  await expect(chat.getByText(/I can do this: Refund \$349\.99 to the Visa ending 4242/)).toBeVisible();
  await expect(chat.getByText(/needs a quick review/)).toBeVisible();

  await input.fill("Yes, please go ahead.");
  await input.press("Enter");
  await expect(chat.getByText(/Submitted for review as APR-/)).toBeVisible();
  await expect(chat.getByText("Waiting for a team member to review it.", { exact: false })).toBeVisible();
  await noSidewaysScroll(page);

  // The operator, in another tab of the same browser: the demo console sees this visitor only.
  const operator = await context.newPage();
  await operator.goto("/console/approvals");
  await operator.getByRole("button", { name: "Open the demo console" }).click();
  await operator.getByRole("button", { name: /APR-\d+/ }).first().click();
  const drawer = operator.getByRole("dialog");
  await expect(drawer.getByText("$349.99", { exact: true })).toBeVisible();
  await drawer.getByLabel("Note").fill("The photo shows the crack.");
  await drawer.getByRole("button", { name: "Approve" }).click();
  await expect(drawer).toBeHidden();
  await noSidewaysScroll(operator);

  // Back in the chat, live over SSE: the card updates and the agent tells the customer.
  await expect(chat.getByText("Good news: our team approved your refund")).toBeVisible();
  await expect(chat.getByText("Approved and done")).toBeVisible();
  await chat.getByRole("button", { name: "Thumbs up" }).click();
  await expect(chat.getByRole("button", { name: "Thumbs up" })).toHaveAttribute("aria-pressed", "true");

  // The replay has the three turns, the tool calls and the approval.
  await operator.getByRole("link", { name: "Conversations" }).click();
  await operator.getByRole("link", { name: /Dana Whitfield/ }).first().click();
  await expect(operator.getByText("Turn 3", { exact: true })).toBeVisible();
  await expect(operator.getByText("issue_refund").first()).toBeVisible();
  await expect(operator.getByRole("link", { name: /APR-\d+/ })).toBeVisible();
  await operator.getByRole("link", { name: "Metrics" }).click();
  await expect(operator.getByText("Resolution rate")).toBeVisible();
});

test("the store and the chat work from the keyboard", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: "Be Leo" }).focus();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "Signed in as Leo" })).toBeVisible();

  await page.getByRole("button", { name: "Chat with support" }).focus();
  await page.keyboard.press("Enter");
  const input = page.getByLabel("Your message");
  await expect(input).toBeFocused();
  await page.keyboard.type("Hello there");
  await page.keyboard.press("Enter");
  await expect(page.getByText(/scripted demo assistant/)).toBeVisible();

  await page.keyboard.press("Escape");
  await expect(page.getByRole("region", { name: "Support chat" })).toBeHidden();
  await expect(page.getByRole("button", { name: "Chat with support" })).toBeVisible();
});
