"""
test_apartments_search.py
─────────────────────────
Isolated diagnostic for the apartments.com smart-search input.

Run from the project root:

    python test_apartments_search.py

What it does
────────────
1. Opens apartments.com (non-headless).
2. Tries every selector format you supplied (CSS, XPath, JS Path),
   plus a few "find any contenteditable inside the smart search"
   probes — and dumps the full element profile for each match
   (tag, id, class, contentEditable, isContentEditable, role,
   bounding rect, outerHTML, etc.).
3. Builds an identity matrix so you can see at a glance which
   selector formats resolve to the *same* DOM node and which point
   somewhere different.
4. Runs every typing strategy against every distinct container,
   reloading the page between strategies for a clean slate.
5. Verifies success by reading textContent / innerText / value /
   innerHTML and looking for the query prefix.
6. Prints a final summary highlighting every (container, strategy)
   pair that succeeded.

Once a working combination is found, plug the winning strategy +
container locator into ``scrapers/apartments_com.py``.
"""

import logging
import random
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import undetected_chromedriver as uc
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.common.exceptions import NoSuchElementException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("smart-search-test")


# ─────────────────────────────────────────────────────────────────────
#  Configuration
# ─────────────────────────────────────────────────────────────────────

BASE_URL = "https://www.apartments.com"
QUERY    = "Montreal, QC, Canada"

CSS_SELECTOR = (
    "#homepage-smart-search > div.smart-search-glow-container "
    "> div > div.smart-search-input.grow"
)
XPATH_SELECTOR = (
    "/html/body/div[1]/div/section[1]/div[1]/section/div/div[1]/div/div[1]"
)
JS_QUERY = (
    "return document.querySelector('"
    "#homepage-smart-search > div.smart-search-glow-container "
    "> div > div.smart-search-input.grow"
    "');"
)

# Additional probes that may resolve to a more focusable child.
EXTRA_PROBES: List[Tuple[str, str]] = [
    ("css_contenteditable_in_root",
     "#homepage-smart-search [contenteditable='true']"),
    ("css_contenteditable_any_attr",
     "#homepage-smart-search [contenteditable]"),
    ("css_smart_search_input_class",
     ".smart-search-input"),
    ("css_smart_search_input_contenteditable_descendant",
     ".smart-search-input [contenteditable]"),
    ("css_role_textbox",
     "#homepage-smart-search [role='textbox']"),
    ("css_input_under_smart_search",
     "#homepage-smart-search input"),
]


# ─────────────────────────────────────────────────────────────────────
#  Browser setup
# ─────────────────────────────────────────────────────────────────────

def create_driver() -> uc.Chrome:
    log.info("Starting Chrome via undetected_chromedriver")
    options = uc.ChromeOptions()
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=en-CA")
    return uc.Chrome(options=options, version_main=148)


def dismiss_popups(driver):
    for sel in (
        "button#onetrust-accept-btn-handler",
        "button.accept-cookies",
        "[aria-label='Close']",
    ):
        try:
            btn = driver.find_element(By.CSS_SELECTOR, sel)
            if btn.is_displayed():
                btn.click()
                time.sleep(1)
                log.info(f"  popup dismissed: {sel}")
        except Exception:
            continue


def reload_clean(driver):
    log.info("  reloading homepage for clean state")
    driver.get(BASE_URL)
    time.sleep(5)
    dismiss_popups(driver)
    time.sleep(1)


# ─────────────────────────────────────────────────────────────────────
#  Locating
# ─────────────────────────────────────────────────────────────────────

def find_css(driver, sel: str):
    try:
        return driver.find_element(By.CSS_SELECTOR, sel)
    except NoSuchElementException:
        return None


def find_xpath(driver, sel: str):
    try:
        return driver.find_element(By.XPATH, sel)
    except NoSuchElementException:
        return None


def find_js(driver, js: str):
    try:
        return driver.execute_script(js)
    except Exception as exc:
        log.error(f"  JS query failed: {exc}")
        return None


def elements_equal(driver, a, b) -> bool:
    if a is None or b is None:
        return False
    try:
        return bool(driver.execute_script(
            "return arguments[0] === arguments[1];", a, b
        ))
    except Exception:
        return False


def describe(driver, el, label: str) -> Optional[Dict[str, Any]]:
    if el is None:
        log.warning(f"  [{label}] → NOT FOUND")
        return None

    info = driver.execute_script("""
        const el = arguments[0];
        const rect = el.getBoundingClientRect();
        const attrs = {};
        for (const a of el.attributes) attrs[a.name] = a.value;
        return {
            tagName:           el.tagName,
            id:                el.id || null,
            className:         el.className || null,
            contentEditable:   el.contentEditable,
            isContentEditable: el.isContentEditable,
            tabIndex:          el.tabIndex,
            role:              el.getAttribute('role'),
            ariaLabel:         el.getAttribute('aria-label'),
            ariaPlaceholder:   el.getAttribute('aria-placeholder'),
            placeholder:       el.getAttribute('placeholder'),
            value:             el.value || null,
            textContent:       (el.textContent || '').slice(0, 100),
            childCount:        el.children.length,
            childTags:         Array.from(el.children).map(c => c.tagName + (c.className?'.'+c.className:'')).slice(0, 5),
            attrs:             attrs,
            visible: rect.width > 0 && rect.height > 0,
            rect:    {x: Math.round(rect.x), y: Math.round(rect.y),
                      w: Math.round(rect.width), h: Math.round(rect.height)},
            outerHTML: (el.outerHTML || '').slice(0, 400),
        };
    """, el)

    log.info(f"  ┌─ [{label}] ─────────────────────────────────────────")
    for k in (
        "tagName", "id", "className", "contentEditable",
        "isContentEditable", "tabIndex", "role", "ariaLabel",
        "ariaPlaceholder", "placeholder", "value",
        "visible", "rect", "childCount", "childTags",
        "textContent",
    ):
        log.info(f"  │  {k:<18s}: {info.get(k)}")
    log.info(f"  │  attrs            : {info.get('attrs')}")
    log.info(f"  │  outerHTML        : {info.get('outerHTML')}")
    log.info(f"  └──────────────────────────────────────────────────────")
    return info


def locate_all(driver) -> Dict[str, Any]:
    log.info("=" * 78)
    log.info("PHASE 1 — Locate smart-search container via multiple formats")
    log.info("=" * 78)

    found: Dict[str, Any] = {}

    log.info("\n▶ CSS Selector:")
    log.info(f"    {CSS_SELECTOR}")
    el = find_css(driver, CSS_SELECTOR)
    describe(driver, el, "css")
    found["css"] = el

    log.info("\n▶ Full XPath:")
    log.info(f"    {XPATH_SELECTOR}")
    el = find_xpath(driver, XPATH_SELECTOR)
    describe(driver, el, "xpath")
    found["xpath"] = el

    log.info("\n▶ JS document.querySelector:")
    el = find_js(driver, JS_QUERY)
    describe(driver, el, "js")
    found["js"] = el

    log.info("\n▶ Extra probes:")
    for name, sel in EXTRA_PROBES:
        log.info(f"   • {name}  ←  {sel}")
        el = find_css(driver, sel)
        describe(driver, el, name)
        found[name] = el

    # ── Identity matrix ───────────────────────────────────────────
    log.info("\n▶ Identity matrix (=== compares DOM node identity):")
    keys = [k for k, v in found.items() if v is not None]
    if len(keys) < 2:
        log.info("   (need ≥2 found elements for comparison)")
    else:
        header = " " * 42 + " | ".join(f"{k[:18]:<18s}" for k in keys)
        log.info("   " + header)
        for a in keys:
            row = f"   {a[:40]:<40s} | "
            for b in keys:
                row += f"{str(elements_equal(driver, found[a], found[b]))[:5]:<18s} | "
            log.info(row)

    return found


def unique_containers(driver, found: Dict[str, Any]) -> List[Tuple[str, Any]]:
    """Collapse same-node aliases — keep one label per unique DOM node."""
    uniques: List[Tuple[str, Any]] = []
    for label, el in found.items():
        if el is None:
            continue
        if any(elements_equal(driver, el, u_el) for _, u_el in uniques):
            continue
        uniques.append((label, el))
    log.info(f"\n  → {len(uniques)} unique container(s) to test")
    return uniques


# ─────────────────────────────────────────────────────────────────────
#  Typing strategies
# ─────────────────────────────────────────────────────────────────────

def s1_actions_individual(driver, container, query):
    """Click via ActionChains, then one ActionChains.send_keys per char."""
    ActionChains(driver).move_to_element(container).click().perform()
    time.sleep(0.8)
    for ch in query:
        ActionChains(driver).send_keys(ch).perform()
        time.sleep(random.uniform(0.06, 0.12))


def s2_container_send_keys(driver, container, query):
    """Click via ActionChains, then container.send_keys() per char."""
    ActionChains(driver).move_to_element(container).click().perform()
    time.sleep(0.8)
    for ch in query:
        container.send_keys(ch)
        time.sleep(random.uniform(0.06, 0.12))


def s3_active_element(driver, container, query):
    """Click via ActionChains, then send_keys to whatever is focused."""
    ActionChains(driver).move_to_element(container).click().perform()
    time.sleep(0.8)
    active = driver.switch_to.active_element
    log.info(
        f"    active element after click: "
        f"<{active.tag_name}> id={active.get_attribute('id')!r} "
        f"class={active.get_attribute('class')!r} "
        f"contenteditable={active.get_attribute('contenteditable')!r}"
    )
    for ch in query:
        active.send_keys(ch)
        time.sleep(random.uniform(0.06, 0.12))


def s4_js_mouse_events_then_actions(driver, container, query):
    """Dispatch mousedown/mouseup/click via JS + focus, then ActionChains."""
    driver.execute_script("""
        const el = arguments[0];
        el.dispatchEvent(new MouseEvent('mousedown', {bubbles:true, cancelable:true}));
        el.dispatchEvent(new MouseEvent('mouseup',   {bubbles:true, cancelable:true}));
        el.dispatchEvent(new MouseEvent('click',     {bubbles:true, cancelable:true}));
        el.focus();
    """, container)
    time.sleep(0.8)
    for ch in query:
        ActionChains(driver).send_keys(ch).perform()
        time.sleep(random.uniform(0.06, 0.12))


def s5_execcommand_insert_text(driver, container, query):
    """Focus + click + document.execCommand('insertText', …)."""
    ActionChains(driver).move_to_element(container).click().perform()
    time.sleep(0.5)
    driver.execute_script(
        "arguments[0].focus(); "
        "document.execCommand('insertText', false, arguments[1]);",
        container, query,
    )
    time.sleep(0.5)


def s6_double_click_actions(driver, container, query):
    """Double-click + individual ActionChains.send_keys."""
    ActionChains(driver).move_to_element(container).double_click().perform()
    time.sleep(0.8)
    for ch in query:
        ActionChains(driver).send_keys(ch).perform()
        time.sleep(random.uniform(0.06, 0.12))


def s7_synthetic_keyboard_events(driver, container, query):
    """Append text to el.textContent + dispatch synthetic keyboard/input
    events.  This is the only strategy that does *not* require trusted
    events — useful as a baseline to confirm React is wired up at all."""
    driver.execute_script(
        "arguments[0].click(); arguments[0].focus();", container
    )
    time.sleep(0.4)
    for ch in query:
        driver.execute_script("""
            const el  = arguments[0];
            const key = arguments[1];
            if (el.isContentEditable) {
                el.textContent = (el.textContent || '') + key;
            }
            const kopts = {
                key: key, code: 'Key' + key.toUpperCase(),
                keyCode: key.charCodeAt(0), which: key.charCodeAt(0),
                bubbles: true, cancelable: true, composed: true,
            };
            el.dispatchEvent(new KeyboardEvent('keydown', kopts));
            el.dispatchEvent(new InputEvent('input', {
                inputType: 'insertText', data: key,
                bubbles: true, cancelable: true, composed: true,
            }));
            el.dispatchEvent(new KeyboardEvent('keyup', kopts));
        """, container, ch)
        time.sleep(0.04)


def s8_find_editable_child_then_send_keys(driver, container, query):
    """If the container has a [contenteditable] descendant, click + type
    on that *child* instead of the container itself."""
    child = driver.execute_script("""
        const el = arguments[0];
        if (el.isContentEditable) return el;
        return el.querySelector('[contenteditable="true"], [contenteditable], input');
    """, container)
    if child is None:
        raise RuntimeError("no contenteditable / input descendant found")
    log.info(
        f"    typing into descendant: <{child.tag_name}> "
        f"class={child.get_attribute('class')!r} "
        f"contenteditable={child.get_attribute('contenteditable')!r}"
    )
    ActionChains(driver).move_to_element(child).click().perform()
    time.sleep(0.8)
    for ch in query:
        ActionChains(driver).send_keys(ch).perform()
        time.sleep(random.uniform(0.06, 0.12))


def s9_single_send_keys_blast(driver, container, query):
    """Click + container.send_keys(entire_query) in one shot."""
    ActionChains(driver).move_to_element(container).click().perform()
    time.sleep(0.8)
    container.send_keys(query)


STRATEGIES: List[Tuple[str, Callable]] = [
    ("S1 actions_individual",              s1_actions_individual),
    # ("S2 container_send_keys",             s2_container_send_keys),
    # ("S3 active_element_send_keys",        s3_active_element),
    # ("S4 js_mouse_events + actions",       s4_js_mouse_events_then_actions),
    # ("S5 execCommand insertText",          s5_execcommand_insert_text),
    # ("S6 double_click + actions",          s6_double_click_actions),
    # ("S7 synthetic KeyboardEvent",         s7_synthetic_keyboard_events),
    # ("S8 editable_child + send_keys",      s8_find_editable_child_then_send_keys),
    # ("S9 single blast send_keys",          s9_single_send_keys_blast),
]


# ─────────────────────────────────────────────────────────────────────
#  Verification
# ─────────────────────────────────────────────────────────────────────

def read_state(driver, container) -> Dict[str, str]:
    return driver.execute_script("""
        const el = arguments[0];
        // Also include the *root* smart-search text so we catch the case
        // where typing appears in a sibling node rather than the
        // container we targeted.
        const root = document.querySelector('#homepage-smart-search') || el;
        return {
            container_textContent: (el.textContent || '').slice(0, 200),
            container_innerText:   (el.innerText   || '').slice(0, 200),
            container_value:       el.value || '',
            container_innerHTML:   (el.innerHTML || '').slice(0, 250),
            root_textContent:      (root.textContent || '').slice(0, 250),
            root_innerHTML:        (root.innerHTML   || '').slice(0, 400),
        };
    """, container)


def verify(driver, container, query: str) -> Tuple[bool, Dict[str, str]]:
    time.sleep(0.7)
    state = read_state(driver, container)
    prefix = query[:6].lower()
    hit = any(prefix in (v or "").lower() for v in state.values())
    return hit, state


# ─────────────────────────────────────────────────────────────────────
#  Main loop
# ─────────────────────────────────────────────────────────────────────

def run_strategy(driver, label: str, locator_fn,
                 strat_name: str, strat_fn) -> Tuple[bool, Dict[str, str]]:
    log.info(f"\n  ── {strat_name}  on  container [{label}]")

    reload_clean(driver)
    container = locator_fn(driver)
    if container is None:
        log.warning(f"     could not re-resolve container [{label}] after reload")
        return False, {}

    pre = read_state(driver, container)
    log.info(f"     pre-state : {pre}")

    try:
        strat_fn(driver, container, QUERY)
    except Exception as exc:
        log.error(f"     strategy raised: {exc.__class__.__name__}: {exc}")
        post = read_state(driver, container)
        log.info(f"     post-state: {post}")
        return False, post

    ok, post = verify(driver, container, QUERY)
    log.info(f"     post-state: {post}")
    log.info(f"     RESULT    : {'✓ SUCCESS' if ok else '✗ fail'}")
    return ok, post


def main():
    driver = create_driver()
    results: List[Tuple[str, str, bool]] = []

    try:
        driver.get(BASE_URL)
        time.sleep(5)
        dismiss_popups(driver)
        time.sleep(1)

        found = locate_all(driver)
        uniques = unique_containers(driver, found)
        if not uniques:
            log.error("No containers found at all — aborting.")
            return

        # Build a locator function for each unique label so we can
        # re-resolve the element after every page reload.
        locators: Dict[str, Callable] = {}
        for label, _ in uniques:
            if label == "css":
                locators[label] = lambda d: find_css(d, CSS_SELECTOR)
            elif label == "xpath":
                locators[label] = lambda d: find_xpath(d, XPATH_SELECTOR)
            elif label == "js":
                locators[label] = lambda d: find_js(d, JS_QUERY)
            else:
                # Find the matching probe selector
                sel = dict(EXTRA_PROBES).get(label)
                locators[label] = (lambda d, s=sel: find_css(d, s))

        # ── Phase 2: run every strategy on every unique container ─
        log.info("\n" + "=" * 78)
        log.info("PHASE 2 — Type into each unique container with every strategy")
        log.info("=" * 78)

        for label, _ in uniques:
            log.info(f"\n▼▼▼ Container: [{label}] ▼▼▼")
            for strat_name, strat_fn in STRATEGIES:
                ok, _ = run_strategy(
                    driver, label, locators[label], strat_name, strat_fn
                )
                results.append((label, strat_name, ok))

        # ── Phase 3: summary ──────────────────────────────────────
        log.info("\n" + "=" * 78)
        log.info("PHASE 3 — Results summary")
        log.info("=" * 78)
        for label, strat_name, ok in results:
            mark = "✓ PASS" if ok else "✗ fail"
            log.info(f"  {mark}  container={label:<40s}  strategy={strat_name}")

        winners = [(l, s) for l, s, ok in results if ok]
        if winners:
            log.info(f"\n🎉  {len(winners)} working combo(s):")
            for l, s in winners:
                log.info(f"      container='{l}'   strategy='{s}'")
        else:
            log.warning("\n⚠️  No combination produced visible text.")
            log.warning("    Browser is staying open for 60s so you can inspect.")
            time.sleep(60)

    finally:
        log.info("\nTest complete. Closing browser in 10s …")
        time.sleep(10)
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    main()