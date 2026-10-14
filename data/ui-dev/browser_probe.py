from pathlib import Path
from playwright.sync_api import sync_playwright

out = Path('data/ui-dev')
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 1600, 'height': 1050}, device_scale_factor=1)
    errors = []
    page.on('pageerror', lambda err: errors.append(str(err)))
    page.goto('http://127.0.0.1:8787/')
    page.wait_for_timeout(1500)
    print('Errors:', errors)
    print('Title:', page.locator('#headline').inner_text())
    print('Dialogs:', page.locator('dialog[open]').count())
    page.screenshot(path=str(out / 'initial-dialog.png'))
    page.keyboard.press('Escape')
    page.screenshot(path=str(out / 'initial-desktop.png'))
    page.locator('#new-engagement').click()
    page.wait_for_timeout(400)
    print('New investigation:', page.locator('#eng-title').inner_text())
    print('Errors after click:', errors)
    page.screenshot(path=str(out / 'new-investigation.png'))
    browser.close()
