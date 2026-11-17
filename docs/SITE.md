# The landing page

One static page in `site/`, for freight brokers deciding whether to ask for a pilot.
It has no build step of its own beyond filling in one address, loads nothing from
other sites, and works with scripts off.

| File | What it is |
| --- | --- |
| `site/index.html` | The page. Three blocks in it are generated (see below) |
| `site/site.css` | Styles. Colours and type follow the broker screens (`chase/app/static/app.css`) |
| `site/site.js` | Sends the signup form in place. Without it the form posts normally |
| `site/claims.json` | What backs each statement on the page. Not published |
| `site/img/`, `site/fonts/` | Screenshots of the product; the Inter typeface (OFL licence beside it) |
| `tools/site.py` | Records the chases, checks them, builds the site |
| `tests/test_site.py` | Holds the page to the product |

## The rule: the page may not say more than the product does

The README says "not yet used by a broker", so the page says so first, in the yellow
strip at the top. Beyond that:

- **The "Run a chase" panel is recorded, not written.** `tools/site.py` runs 18
  chases (3 delivery times, 6 carrier behaviours) through the real engine with the
  stand-in channels the tests use, and writes what happened into the page: times from
  the schedule, wording from the broker screens' timeline, hashes from the evidence
  chain. The email shown is the real template; only its one-time upload link is
  shortened. Choosing a chase is plain CSS (radio buttons and `:has()`), so it works
  with scripts off.
- **Every statement is listed in `site/claims.json`** under the `data-claim` name it
  carries on the page, with a test that must exist or words that must still be in a
  document. A statement not listed, or a listed one whose backing is gone, fails
  `tests/test_site.py`.
- **No invented proof.** No logos, quotes, customer counts, percentages or prices. A
  test refuses those words.

Three statements are promises about how the business behaves, which no test can check.
Only the owner can keep them, so read them before publishing:

| Name | The page says |
| --- | --- |
| `pilot-price` | The price is not set. Change this line when it is |
| `privacy` | Signup details are used only to reply about the pilot, are not shared, and are deleted on request |
| `notyet-reader` | Every document comes to a person until the reader has been measured on real PODs. True without `ANTHROPIC_API_KEY`; with it, only while the broker's confidence floor is 100 ([READER.md](READER.md)) |

## Changing it

```bash
python tools/site.py runs            # after changing the engine, schedule or timeline wording
python tools/site.py runs --check    # what CI runs
pytest tests/test_site.py
```

To change the words, edit `site/index.html` outside the three generated blocks
(`runs`, `email`, `chain`, each between `<!-- name:start -->` and `<!-- name:end -->`).
A new statement needs a `data-claim` name and an entry in `site/claims.json`. When
something on the "Not built" list is built, move it only after the README's "What is
not built" section has changed; a test compares the two.

To retake the screenshots after the broker screens change:

```bash
python -m chase.app.preview          # writes out/screens/needs-you.html
```

Open `out/screens/needs-you.html` in a browser and save `needs-you.webp` (2400 by
1520, a 1200 by 760 window at double density), `needs-you-1200.webp` (the same at
1200 wide) and `needs-you-phone.webp` (800 by 1400, a 400 by 700 window at double
density) into `site/img/`, each under 120 KB.

## Publishing

Nothing here has been published yet. Three things are needed, in this order.

**1. Somewhere for the form to post.** The page is static, so signups go to a form
service that accepts a normal form post and emails you each one (Formspree is one;
any equivalent works). Create a form there and copy its address, which looks like
`https://formspree.io/f/abcdwxyz`. The repo never holds this address:
`site/index.html` carries a stand-in, and the build refuses to run without the real
one, so a page with a dead form cannot be published.

The form sends `email`, `company`, `name`, `loads_per_month`, `tms` and `note`. A
hidden `_gotcha` field is left empty by people and filled by bots; most form services
drop a post that fills it.

**2. Build.**

```bash
python tools/site.py build --form-url https://formspree.io/f/abcdwxyz --out dist
```

`dist/` is the whole site: put it on any static host. `--inline` writes one
self-contained file instead, for previews.

**3. Host.** `.github/workflows/site.yml` publishes to GitHub Pages on every push to
`main` that touches the page:

1. Repository Settings, Pages: set the source to "GitHub Actions".
2. Repository Settings, Secrets and variables, Actions, Variables: add
   `SIGNUP_FORM_URL` with the form address. Until it is set the workflow skips.
3. For your own address (for example `chase.talonsight.com`): add a `CNAME` record at
   the DNS host pointing that name to `jsama99.github.io`, then enter the name under
   Settings, Pages, Custom domain.

Any other static host works the same way: run the build command with the form
address and publish `dist/`.

## Not verified

- **The form service.** The script sends the form with `Accept: application/json` and
  treats any 2xx answer as success, which is the documented behaviour of the common
  services, but no real form has received a post from this page. After publishing,
  send one signup with scripts on and one with scripts off.
- **GitHub Pages.** The workflow has not run, and the action versions in it were not
  checked against the latest releases.
- **Browsers.** The page was looked at in Chrome at phone and desktop widths, with and
  without reduced motion. It was not opened in Safari or Firefox. Browsers without
  `:has()` show the first recorded chase and hide the choices.
