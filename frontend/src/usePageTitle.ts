/**
 * Name the tab after the screen.
 *
 * Every route rendered as "Banking Platform", which is fine until there are three tabs open during
 * a demo, or a bookmark, or a browser-history entry — all of which are just the title in a
 * different font. A single-page app has to set this itself; there is no server sending a document
 * per page.
 *
 * Deliberately not react-helmet: this is one `useEffect` and a string, and the library exists to
 * solve ordering problems for a `<head>` full of competing tags. There is one tag here.
 */

import { useEffect } from "react";

const SUFFIX = "Banking Platform";

/**
 * Set `document.title` to `page — Banking Platform` for as long as the caller is mounted.
 *
 * Pass `null` while the name is still loading — an account's title needs the account — and the
 * previous title stays up rather than flashing "undefined" for a frame.
 */
export function usePageTitle(page: string | null): void {
  useEffect(() => {
    if (page === null) return;

    const previous = document.title;
    document.title = page === SUFFIX ? SUFFIX : `${page} — ${SUFFIX}`;
    // Restoring on unmount matters for the back button: without it, navigating away from a screen
    // that never sets a title (a 404, say) would leave the previous screen's name in the tab.
    return () => {
      document.title = previous;
    };
  }, [page]);
}
