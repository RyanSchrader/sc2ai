const API_ROOT = "/api";

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_ROOT}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers ?? {}),
    },
  });
  const contentType = response.headers.get("content-type") ?? "";
  const rawBody = await response.text();
  const receivedHtml =
    contentType.includes("text/html") ||
    rawBody.trimStart().toLowerCase().startsWith("<!doctype");

  let body: unknown;
  if (rawBody) {
    try {
      body = JSON.parse(rawBody);
    } catch {
      if (receivedHtml) {
        throw new ApiError(
          502,
          "Bot Studio API is unavailable: the frontend received HTML instead of JSON. Restart the development server with make dev.",
        );
      }
      throw new ApiError(
        response.ok ? 502 : response.status,
        "Bot Studio API returned an invalid response.",
      );
    }
  }

  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    if (
      typeof body === "object" &&
      body !== null &&
      "detail" in body &&
      typeof body.detail === "string"
    ) {
      message = body.detail;
    }
    throw new ApiError(response.status, message);
  }
  return body as T;
}

export const jsonOptions = (method: string, body?: unknown): RequestInit => ({
  method,
  body: body === undefined ? undefined : JSON.stringify(body),
});
