import { beforeEach, describe, expect, it } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigationType } from "react-router";
import { QueryClientProvider } from "@tanstack/react-query";
import OpenPage, { keysFromSearch, readerPath } from "./OpenPage";
import ProtectedRoute from "@/components/ProtectedRoute";
import { resetSessionExpiryForTests } from "@/auth/expiry";
import { makeMe, makeQueryClient, mockFetch } from "@/test/utils";

const SHA = "ab".repeat(32);

const MATCH = {
  attachment_id: "att-1",
  filename: "widgets.pdf",
  item_id: "item-1",
  space_id: "space-1",
};

/** Where the router ended up, and whether it got there by replacing. */
function LocationProbe() {
  const loc = useLocation();
  const how = useNavigationType();
  return (
    <div data-testid="at" data-navigation={how}>{`${loc.pathname}${loc.search}`}</div>
  );
}

/**
 * /open behind the guard, as App.tsx mounts it, with the reader and the
 * login page as probes. The address bar is set too: the guard's `next`
 * is read from `window.location`, not from the router.
 */
function renderOpen(route: string) {
  window.history.replaceState(null, "", route);
  return render(
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={[route]}>
        <Routes>
          <Route
            path="/open"
            element={
              <ProtectedRoute>
                <OpenPage />
              </ProtectedRoute>
            }
          />
          <Route path="/reader/:attachmentId" element={<LocationProbe />} />
          <Route path="/login" element={<LocationProbe />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function requestedUrls(fn: ReturnType<typeof mockFetch>): string[] {
  return fn.mock.calls.map((c) => String(c[0]));
}

beforeEach(() => {
  resetSessionExpiryForTests();
  window.history.replaceState(null, "", "/");
});

describe("OpenPage", () => {
  it("replaces itself with the reader at the linked page and search", async () => {
    const fetchMock = mockFetch({
      "/api/me": { body: makeMe() },
      [`/api/attachments/resolve?sha256=${SHA}`]: { body: { attachments: [MATCH] } },
    });
    renderOpen(`/open?sha256=${SHA}&page=57&find=Table%207-1`);

    const at = await screen.findByTestId("at");
    expect(at).toHaveTextContent("/reader/att-1?page=57&find=Table+7-1");
    expect(at).toHaveAttribute("data-navigation", "REPLACE");
    expect(requestedUrls(fetchMock)).toContain(
      `/api/attachments/resolve?sha256=${SHA}`,
    );
  });

  it("takes the oldest match when the file is in several spaces", async () => {
    mockFetch({
      "/api/me": { body: makeMe() },
      "/api/attachments/resolve": {
        body: { attachments: [MATCH, { ...MATCH, attachment_id: "att-2" }] },
      },
    });
    renderOpen(`/open?sha256=${SHA}`);
    expect(await screen.findByTestId("at")).toHaveTextContent(/^\/reader\/att-1$/);
  });

  it("falls back to the edition when the hash finds nothing", async () => {
    const fetchMock = mockFetch({
      "/api/me": { body: makeMe() },
      "/api/attachments/resolve?sha256=": { body: { attachments: [] } },
      "/api/attachments/resolve?body=": { body: { attachments: [MATCH] } },
    });
    renderOpen(
      `/open?sha256=${SHA}&body=NX+Standards&designation=NX-ACME+1234&label=2020&page=3`,
    );

    expect(await screen.findByTestId("at")).toHaveTextContent("/reader/att-1?page=3");
    expect(requestedUrls(fetchMock)).toContain(
      "/api/attachments/resolve?body=NX+Standards&designation=NX-ACME+1234&label=2020",
    );
  });

  it("says the document is not in the library when nothing matches", async () => {
    mockFetch({
      "/api/me": { body: makeMe() },
      "/api/attachments/resolve": { body: { attachments: [] } },
    });
    renderOpen(`/open?sha256=${SHA}&page=5`);

    expect(
      await screen.findByRole("heading", { name: "Not in your library" }),
    ).toBeInTheDocument();
    expect(screen.getByText(SHA)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Go to your library" })).toHaveAttribute(
      "href",
      "/library",
    );
    expect(screen.queryByTestId("at")).not.toBeInTheDocument();
  });

  it("asks for a key rather than looking anything up when the link has none", async () => {
    const fetchMock = mockFetch({ "/api/me": { body: makeMe() } });
    renderOpen("/open?sha256=not-a-hash&page=5");

    expect(
      await screen.findByRole("heading", { name: "Incomplete link" }),
    ).toBeInTheDocument();
    expect(
      requestedUrls(fetchMock).some((u) => u.startsWith("/api/attachments")),
    ).toBe(false);
  });

  it("treats a key the server rejects as an incomplete link", async () => {
    mockFetch({
      "/api/me": { body: makeMe() },
      "/api/attachments/resolve": {
        status: 422,
        body: { detail: "Give sha256, or all of body, designation and label" },
      },
    });
    renderOpen(`/open?sha256=${SHA}`);

    expect(
      await screen.findByRole("heading", { name: "Incomplete link" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("reports any other failure as an error", async () => {
    mockFetch({
      "/api/me": { body: makeMe() },
      "/api/attachments/resolve": { status: 500, body: {} },
    });
    renderOpen(`/open?sha256=${SHA}`);

    expect(
      await screen.findByRole("heading", { name: "Couldn't look the document up" }),
    ).toBeInTheDocument();
  });

  it("sends a signed-out visitor through login and back to the same link", async () => {
    mockFetch({ "/api/me": { status: 401, body: { detail: "Not authenticated" } } });
    renderOpen(`/open?sha256=${SHA}&page=57`);

    await waitFor(() =>
      expect(screen.getByTestId("at")).toHaveTextContent(
        `/login?next=${encodeURIComponent(`/open?sha256=${SHA}&page=57`)}`,
      ),
    );
  });
});

describe("keysFromSearch", () => {
  it("tries the hash before the edition", () => {
    const keys = keysFromSearch(
      new URLSearchParams(`sha256=${SHA}&body=B&designation=D&label=L`),
    );
    expect(keys).toEqual([
      { sha256: SHA },
      { body: "B", designation: "D", label: "L" },
    ]);
  });

  it("drops an edition field longer than the server accepts", () => {
    const at = (body: number, designation: number, label: number) =>
      keysFromSearch(
        new URLSearchParams({
          body: "b".repeat(body),
          designation: "d".repeat(designation),
          label: "l".repeat(label),
        }),
      ).length;
    expect(at(120, 200, 120)).toBe(1);
    expect(at(121, 200, 120)).toBe(0);
    expect(at(120, 201, 120)).toBe(0);
    expect(at(120, 200, 121)).toBe(0);
  });

  it("drops an edition missing a field", () => {
    expect(keysFromSearch(new URLSearchParams("body=B&designation=D"))).toEqual([]);
  });
});

describe("readerPath", () => {
  it("keeps a usable page and the search term, and nothing else", () => {
    expect(
      readerPath("a b", new URLSearchParams(`sha256=${SHA}&page=4&find=x&foo=1`)),
    ).toBe("/reader/a%20b?page=4&find=x");
  });

  it("drops a page that isn't a page number", () => {
    expect(readerPath("a", new URLSearchParams("page=0"))).toBe("/reader/a");
    expect(readerPath("a", new URLSearchParams("page=two"))).toBe("/reader/a");
  });
});
