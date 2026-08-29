"""Tests cahier des charges :
  #7 generation TypeScript de chaque action
  #8 exclusion des actions marquees comme parasites
  #9 protection des valeurs sensibles (env var, jamais en clair)
"""
import pytest
from pathlib import Path

from schemas import CleanSteps, Step, Selector
from playwright_generator import generate_playwright_ts, UnsupportedAction, _emit_step_body


def _wrap(steps):
    return CleanSteps(parcours="test", scenario_url="https://example.com",
                      total_steps=len(steps), steps=steps)


@pytest.fixture
def out_dir(tmp_path):
    return tmp_path


def test_navigate_emits_page_goto(out_dir):
    """#7 : navigate -> page.goto(url)."""
    cs = _wrap([Step(id="step-0001", step=1, action="navigate", url="https://example.com/login")])
    r = generate_playwright_ts(cs, out_dir / "test.spec.ts")
    body = Path(r["path"]).read_text(encoding="utf-8")
    assert 'await page.goto("https://example.com/login");' in body


def test_click_emits_locator_click_without_first(out_dir):
    """#7 : click -> locator(sel).click() (SANS .first() : le strict mode
    Playwright doit crasher si le selecteur est ambigu, pas cliquer
    silencieusement sur le premier match)."""
    cs = _wrap([Step(id="step-0001", step=1, action="click",
                     selector=Selector(value="#login", strategy="id", unique=True))])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert 'page.locator("#login").click()' in body
    assert '.first().click()' not in body


def test_shadow_dom_chain_splits_into_nested_locators(out_dir):
    """Shadow DOM : les chaines 'host >>> inner' sont splittees en
    .locator().locator() (syntaxe Playwright officielle, les shadow
    roots ouverts sont traverses automatiquement) plutot que passees
    littarelement a page.locator() qui ne comprendrait pas '>>>'."""
    cs = _wrap([Step(id="step-0001", step=1, action="click",
                     selector=Selector(value="my-widget >>> [aria-label='Submit']",
                                       strategy="shadow", inShadowDOM=True))])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    # Doit chainer .locator() et NE PAS contenir la string ">>>"
    assert 'page.locator("my-widget").locator("[aria-label=\'Submit\']")' in body
    assert ">>>" not in body


def test_deep_shadow_dom_chain(out_dir):
    """Shadow DOM 3 niveaux : host1 >>> host2 >>> inner"""
    cs = _wrap([Step(id="step-0001", step=1, action="click",
                     selector=Selector(value="app-root >>> nav-bar >>> #logout",
                                       strategy="shadow", inShadowDOM=True))])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert 'page.locator("app-root").locator("nav-bar").locator("#logout")' in body


def test_input_non_sensitive_uses_fill_with_value_literal(out_dir):
    """#7 : input non-sensitive -> locator.fill('valeur')."""
    cs = _wrap([Step(id="step-0001", step=1, action="input",
                     selector=Selector(value="input[name='q']", strategy="name"),
                     value="hello world", sensitive=False)])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert 'fill("hello world")' in body


def test_input_sensitive_uses_process_env(out_dir):
    """#9 : sensitive: true -> process.env, JAMAIS la valeur en clair."""
    cs = _wrap([Step(id="step-0001", step=1, action="input",
                     selector=Selector(value="input[type='password']", strategy="css"),
                     value="realpassword123",  # sera reference mais pas ecrit
                     sensitive=True, env_var="DOMAUTOPSY_TEST_PWD")])
    r = generate_playwright_ts(cs, out_dir / "t.spec.ts")
    body = Path(r["path"]).read_text()
    assert "realpassword123" not in body, "valeur en clair fuite dans le TS"
    assert "process.env.DOMAUTOPSY_TEST_PWD" in body
    assert "DOMAUTOPSY_TEST_PWD" in r["sensitive_vars"]


def test_verify_variants_emit_expect(out_dir):
    """#7 : verify avec differents types -> expect() adapte."""
    steps = [
        Step(id="step-0001", step=1, action="verify", verify_type="texte_contient",
             expected="Bienvenue"),
        Step(id="step-0002", step=2, action="verify", verify_type="visible",
             selector=Selector(value=".welcome", strategy="css")),
        Step(id="step-0003", step=3, action="verify", verify_type="absent",
             selector=Selector(value=".error", strategy="css")),
    ]
    body = Path(generate_playwright_ts(_wrap(steps), out_dir / "t.spec.ts")["path"]).read_text()
    # getByText garde .first() car un mot peut apparaitre N fois sur la
    # page et on veut verifier qu'au moins une occurrence est visible -
    # sans .first() Playwright leverait "strict mode violation" sur du
    # texte legitiment repete (ex. header + footer). Distinction avec
    # locator(css) ou l'ambiguite reflete un vrai probleme QA.
    assert 'expect(page.getByText("Bienvenue").first()).toBeVisible()' in body
    assert 'expect(page.locator(".welcome")).toBeVisible()' in body
    assert 'expect(page.locator(".error")).toHaveCount(0)' in body


def test_scroll_delta_uses_mouse_wheel(out_dir):
    """#7 : scroll delta -> page.mouse.wheel."""
    cs = _wrap([Step(id="step-0001", step=1, action="scroll", direction="down", deltaY=500)])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "page.mouse.wheel(0, 500)" in body


def test_scroll_to_element_uses_scrollIntoView(out_dir):
    """#7 : scroll vers element -> scrollIntoViewIfNeeded."""
    cs = _wrap([Step(id="step-0001", step=1, action="scroll", direction="vers_element",
                     selector=Selector(value="#footer", strategy="id"))])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "scrollIntoViewIfNeeded" in body


def test_wait_temporel_emits_waitForTimeout(out_dir):
    cs = _wrap([Step(id="step-0001", step=1, action="wait", seconds=2.5)])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "page.waitForTimeout(2500)" in body


def test_wait_selector_emits_waitFor(out_dir):
    cs = _wrap([Step(id="step-0001", step=1, action="wait",
                     selector=Selector(value=".loader", strategy="css"),
                     wait_state="hidden")])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert 'waitFor({ state: "hidden" })' in body


def test_cookie_action_wraps_in_try_catch(out_dir):
    """#7 : cookie conditionnel, ne doit pas faire echouer le test si absent."""
    cs = _wrap([Step(id="step-0001", step=1, action="cookie",
                     selector=Selector(value="#accept-cookies", strategy="id"))])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "try {" in body
    assert "cookie banner absente" in body


def test_keyboard_emits_press(out_dir):
    cs = _wrap([Step(id="step-0001", step=1, action="keyboard", value="Enter")])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert 'page.keyboard.press("Enter")' in body


def test_upload_emits_setInputFiles(out_dir):
    cs = _wrap([Step(id="step-0001", step=1, action="upload",
                     selector=Selector(value="input[type='file']", strategy="css"),
                     value="/tmp/f.txt")])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert 'setInputFiles("/tmp/f.txt")' in body


def test_switch_tab_uses_context_pages_index(out_dir):
    """switch_tab -> page.context().pages()[N] + bringToFront. Deterministe
    par ORDRE DE CREATION (pas tab_id opaque BU)."""
    cs = _wrap([Step(id="step-0001", step=1, action="switch_tab", value="2")])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "page.context().pages()" in body
    assert "_pages[2]" in body
    assert "bringToFront" in body


def test_switch_tab_throws_when_index_missing(out_dir):
    """Guard runtime : si l'onglet cible n'existe pas, throw explicite
    plutot que crash silencieux."""
    cs = _wrap([Step(id="step-0001", step=1, action="switch_tab", value="5")])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "throw new Error" in body
    assert "5" in body


def test_close_tab_current_when_no_value(out_dir):
    """close_tab sans value -> ferme la page courante et repointe sur pages()[0]."""
    cs = _wrap([Step(id="step-0001", step=1, action="close_tab")])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "await page.close()" in body
    assert "_pages[0]" in body


def test_close_tab_by_index_when_value_given(out_dir):
    """close_tab avec value -> ferme _pages[index] + repointe sur restant[0]."""
    cs = _wrap([Step(id="step-0001", step=1, action="close_tab", value="1")])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "_pages[1].close()" in body
    assert "_remaining" in body


def test_extract_emitter_throws_explicit_error(out_dir):
    """extract garde-fou : si un step extract arrive included_in_replay=True,
    le TS leve un throw explicite (pas un no-op silencieux)."""
    cs = _wrap([Step(
        id="step-0001", step=1, action="extract",
        description="Lit le titre",
        included_in_replay=True,  # cas anormal - devrait etre False
    )])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert "throw new Error" in body
    assert "non rejouable" in body


def test_extract_marked_not_replayable_is_skipped_in_ts(out_dir):
    """Cas normal : extract avec included_in_replay=False -> SKIPPED
    commentaire dans le TS, pas de code exécutable."""
    cs = _wrap([Step(
        id="step-0001", step=1, action="extract",
        description="Lit le titre",
        included_in_replay=False,
        cleanup_reason="action extract non rejouable",
    )])
    r = generate_playwright_ts(cs, out_dir / "t.spec.ts")
    body = Path(r["path"]).read_text()
    assert "// SKIPPED [step-0001]" in body
    assert "throw new Error" not in body
    assert r["skipped_count"] == 1


def test_go_back_reload_open_tab(out_dir):
    steps = [
        Step(id="step-0001", step=1, action="go_back"),
        Step(id="step-0002", step=2, action="reload"),
        Step(id="step-0003", step=3, action="open_tab", url="https://a.com"),
    ]
    body = Path(generate_playwright_ts(_wrap(steps), out_dir / "t.spec.ts")["path"]).read_text()
    assert "page.goBack()" in body
    assert "page.reload()" in body
    assert 'newPage.goto("https://a.com")' in body


def test_excluded_step_is_commented_not_executed(out_dir):
    """#8 : included_in_replay=false -> commentaire SKIPPED, jamais un
    statement executable. Le step reste visible pour la tracabilite."""
    steps = [
        Step(id="step-0001", step=1, action="click",
             selector=Selector(value="#a", strategy="id"), included_in_replay=True),
        Step(id="step-0002", step=2, action="click",
             selector=Selector(value=".overlay", strategy="css"),
             included_in_replay=False,
             cleanup_reason="clic parasite sur overlay modal"),
        Step(id="step-0003", step=3, action="click",
             selector=Selector(value="#b", strategy="id"), included_in_replay=True),
    ]
    r = generate_playwright_ts(_wrap(steps), out_dir / "t.spec.ts")
    body = Path(r["path"]).read_text()
    assert "// SKIPPED [step-0002]" in body
    assert "clic parasite sur overlay modal" in body
    assert 'page.locator(".overlay").first().click()' not in body
    assert r["included_count"] == 2
    assert r["skipped_count"] == 1


def test_unsupported_action_emits_throw_and_reports(out_dir):
    """Cahier : "Si une action ne peut pas etre traduite, marque-la comme
    non executable dans le JSON et le rapport, et fais echouer clairement
    la generation ou le replay selon le cas."
    """
    cs = _wrap([Step(id="step-0001", step=1, action="totally_unknown_action")])
    r = generate_playwright_ts(cs, out_dir / "t.spec.ts")
    body = Path(r["path"]).read_text()
    assert "throw new Error(" in body
    assert len(r["unsupported"]) == 1
    assert r["unsupported"][0]["step_id"] == "step-0001"


def test_generated_file_uses_test_step_with_stable_id(out_dir):
    """Rapprochement JSON<->Playwright : chaque etape doit etre encapsulee
    dans test.step("[step-XXXX] ACTION - desc", ...)."""
    cs = _wrap([Step(id="step-0042", step=42, action="click", description="clic Login",
                     selector=Selector(value="#login", strategy="id"))])
    body = Path(generate_playwright_ts(cs, out_dir / "t.spec.ts")["path"]).read_text()
    assert 'test.step("[step-0042] CLICK - clic Login"' in body
