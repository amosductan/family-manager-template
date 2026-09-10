"""Fail if any page scrolls sideways on a phone.

Both parents read this hub on a phone, so "does it fit the screen" is a correctness
property, not a nicety — and it regresses invisibly, because every one of these pages
looks fine on a laptop while being 170px too wide on a phone.

    python check_mobile.py                          # a local server on the default port
    python check_mobile.py http://127.0.0.1:5088    # any running hub

Exit 0 = every page fits at every width. Exit 1 = it names the widths, the pages and
the specific elements sticking out past the edge.

Needs `pip install playwright` + `playwright install chromium`.
"""
import sys
from urllib.parse import quote

from playwright.sync_api import sync_playwright

import family

DEFAULT_BASE = "http://127.0.0.1:5088"
# One page per kid in the household, so a long name gets tested on the page it lives on.
PAGES = (["/", "/actions", "/actions?view=review", "/weekly-review", "/payment-plans"]
         + [f"/kid/{quote(k)}" for k in family.KIDS]
         + ["/mail", "/mail/sources", "/mail/add", "/days-off", "/checklist", "/trip",
            "/events", "/sitters", "/scan", "/trips", "/gatherings", "/payments", "/ask",
            "/privacy"])
# 320 = smallest phone still in use; 768 = the tablet width just under the 720px
# breakpoint's neighbors; 1280 = the desktop layout must not regress either.
WIDTHS = [320, 360, 390, 430, 540, 768, 900, 1280]

PROBE = """
() => {
  const de = document.documentElement;
  const vw = de.clientWidth;
  const out = [];
  document.querySelectorAll('body *').forEach(el => {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;
    const over = Math.round(r.right + window.scrollX - vw);
    if (over > 1) {
      const cls = el.className;
      out.push({
        over,
        w: Math.round(r.width),
        tag: el.tagName.toLowerCase(),
        cls: (cls && cls.baseVal !== undefined ? cls.baseVal : cls) || '',
        txt: (el.textContent || '').trim().slice(0, 40).replace(/\\s+/g, ' ')
      });
    }
  });
  out.sort((a, b) => b.over - a.over);
  return {vw, sw: de.scrollWidth, over: out.slice(0, 10), total: out.length};
}
"""


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else DEFAULT_BASE).rstrip("/")
    failures = 0
    with sync_playwright() as p:
        b = p.chromium.launch()
        for w in WIDTHS:
            pg = b.new_page(viewport={"width": w, "height": 844},
                            is_mobile=w < 900, has_touch=w < 900)
            bad = 0
            for path in PAGES:
                response = pg.goto(base + path, wait_until="load", timeout=45000)
                if response is None or response.status >= 400:
                    bad += 1
                    print(f"HTTP FAILURE @{w}px {path}: {response.status if response else 'no response'}")
                    continue
                r = pg.evaluate(PROBE)
                if r["sw"] > r["vw"] + 1:
                    bad += 1
                    print(f"\nOVERFLOW @{w}px {path}  scrollWidth={r['sw']} "
                          f"viewport={r['vw']}  ({r['total']} elements past the edge)")
                    for o in r["over"]:
                        cls = f".{o['cls'].split()[0]}" if o["cls"] else ""
                        print(f"    +{o['over']:>4}px  <{o['tag']}{cls}> "
                              f"w={o['w']}  {o['txt']!r}")
            failures += bad
            print(f"{'PASS' if not bad else 'FAIL'}  {w}px  "
                  f"{len(PAGES) - bad}/{len(PAGES)} pages fit without sideways scroll")
            pg.close()
        b.close()
    print("\n" + ("ALL CLEAR" if not failures
                  else f"{failures} page/width combinations scroll sideways"))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
