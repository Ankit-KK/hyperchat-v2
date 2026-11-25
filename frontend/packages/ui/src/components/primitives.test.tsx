import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import {
  Alert,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
  DialogTrigger,
  Input,
  Label,
} from "../index";

describe("Button", () => {
  it("is a non-submitting button by default and runs its handler", async () => {
    const onClick = vi.fn();
    render(<Button onClick={onClick}>Save</Button>);
    const button = screen.getByRole("button", { name: "Save" });
    expect(button).toHaveAttribute("type", "button");
    await userEvent.click(button);
    expect(onClick).toHaveBeenCalledOnce();
  });

  it("does nothing when disabled", async () => {
    const onClick = vi.fn();
    render(
      <Button disabled onClick={onClick}>
        Save
      </Button>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(onClick).not.toHaveBeenCalled();
  });

  it("styles its child with asChild, keeping link semantics", () => {
    render(
      <Button asChild>
        <a href="/x">Go</a>
      </Button>,
    );
    expect(screen.getByRole("link", { name: "Go" })).toHaveClass("rounded-md");
    expect(screen.queryByRole("button")).toBeNull();
  });
});

describe("Label and Input", () => {
  it("labels the input and focuses it on click", async () => {
    render(
      <>
        <Label htmlFor="email">Email</Label>
        <Input id="email" type="email" />
      </>,
    );
    const input = screen.getByLabelText("Email");
    await userEvent.click(screen.getByText("Email"));
    expect(input).toHaveFocus();
  });

  it("marks an invalid input for assistive technology", () => {
    render(
      <>
        <Input aria-label="Slug" invalid aria-describedby="e" />
        <p id="e">Taken</p>
      </>,
    );
    const input = screen.getByRole("textbox", { name: "Slug" });
    expect(input).toBeInvalid();
    expect(input).toHaveAccessibleDescription("Taken");
  });
});

describe("Dialog", () => {
  it("opens with an accessible name, holds focus, closes on Escape and returns focus", async () => {
    render(
      <Dialog>
        <DialogTrigger>Open</DialogTrigger>
        <DialogContent>
          <DialogTitle>Delete sound</DialogTitle>
          <DialogDescription>This cannot be undone.</DialogDescription>
        </DialogContent>
      </Dialog>,
    );
    const trigger = screen.getByRole("button", { name: "Open" });
    await userEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "Delete sound" });
    expect(dialog).toHaveAccessibleDescription("This cannot be undone.");
    expect(dialog).toContainElement(document.activeElement as HTMLElement);
    await userEvent.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(trigger).toHaveFocus();
  });

  it("has a labelled close button", async () => {
    render(
      <Dialog defaultOpen>
        <DialogContent closeLabel="Close dialog">
          <DialogTitle>Title</DialogTitle>
          <DialogDescription>Text</DialogDescription>
        </DialogContent>
      </Dialog>,
    );
    await userEvent.click(screen.getByRole("button", { name: "Close dialog" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

describe("Alert", () => {
  it("uses role=alert for errors and role=status otherwise", () => {
    render(
      <>
        <Alert tone="error">Failed</Alert>
        <Alert>Saved</Alert>
      </>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Failed");
    expect(screen.getByRole("status")).toHaveTextContent("Saved");
  });
});
