import runpy
from playwright.sync_api import sync_playwright

fixtures = runpy.run_path('tests/browser/test_web_ui.py')['fixture_data']()
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={'width': 1600, 'height': 1050})
    errors = []
    policy_errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda msg: policy_errors.append(msg.text) if 'Content Security Policy' in msg.text else None)
    def route_api(route):
        name = route.request.url.split('/api/v1/')[1].split('?')[0]
        if name == 'events':
            route.fulfill(content_type='text/event-stream', body=': UI fixture\n\n')
        else:
            route.fulfill(json=fixtures.get(name, {}))
    page.route('**/api/v1/**', route_api)
    response = page.goto('http://127.0.0.1:8787/')
    page.locator('.agent-row').first.wait_for()
    page.locator('.nav-item[data-view=graph]').click()
    page.locator('[data-node="task:t1"]').wait_for()
    task = page.locator('[data-node="task:t1"]').bounding_box()
    worker = page.locator('[data-node="worker:worker-1"]').bounding_box()
    assert worker['x'] > task['x'] and abs(worker['y'] - task['y']) < 1
    assert errors == [], errors
    assert policy_errors == [], policy_errors
    print('Real server CSP active:', bool(response.headers.get('content-security-policy')))
    print('Script errors:', errors, 'Policy errors:', policy_errors)
    page.screenshot(path='data/ui-dev/graph-overview.png')
    page.locator('[data-node="task:t1"]').click()
    page.screenshot(path='data/ui-dev/graph-selected.png')
    browser.close()
