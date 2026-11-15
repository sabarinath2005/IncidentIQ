import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { MemoryRouter } from "react-router-dom";
import App from "./App";

afterEach(() => {
  localStorage.clear();
  cleanup();
});

describe("authentication screen", () => {
  it("shows sign in and switches to registration", async () => {
    render(<MemoryRouter><App/></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "Sign in to IncidentIQ" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));
    expect(screen.getByRole("heading", { name: "Start your workspace" })).toBeInTheDocument();
    expect(screen.getByLabelText("Full name")).toBeInTheDocument();
  });
});
