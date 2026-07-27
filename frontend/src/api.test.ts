import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api } from "./api";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("api", () => {
  it("returns parsed JSON responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ bots: 8 }), {
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(api<{ bots: number }>("/bots")).resolves.toEqual({ bots: 8 });
  });

  it("uses API error details from JSON responses", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response(JSON.stringify({ detail: "Bot not found" }), {
          status: 404,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );

    await expect(api("/bots/missing")).rejects.toMatchObject({
      status: 404,
      message: "Bot not found",
    } satisfies Partial<ApiError>);
  });

  it("explains when the frontend receives HTML instead of API JSON", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(
        new Response("<!doctype html><html></html>", {
          headers: { "Content-Type": "text/html" },
        }),
      ),
    );

    await expect(api("/bots")).rejects.toMatchObject({
      status: 502,
      message:
        "Bot Studio API is unavailable: the frontend received HTML instead of JSON. Restart the development server with make dev.",
    } satisfies Partial<ApiError>);
  });
});
