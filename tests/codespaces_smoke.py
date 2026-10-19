"""HTTP health + CSRF-protected admin/admin login in the isolated CI container."""
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request

base = 'http://127.0.0.1:8000'
opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
for attempt in range(40):
    try:
        with opener.open(base + '/health/', timeout=2) as response:
            assert response.status == 200
        break
    except (urllib.error.URLError, TimeoutError):
        if attempt == 39:
            raise
        time.sleep(1)
with opener.open(base + '/login/', timeout=5) as response:
    page = response.read().decode()
token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page).group(1)
request = urllib.request.Request(base + '/login/', data=urllib.parse.urlencode({
    'username': 'admin', 'password': 'admin', 'csrfmiddlewaretoken': token,
}).encode(), headers={'Origin': base})
with opener.open(request, timeout=10) as response:
    assert urllib.parse.urlparse(response.url).path != '/login/', 'Demo admin login failed'
    assert 'Tickets' in response.read().decode()
with opener.open(base + '/admin/', timeout=10) as response:
    assert urllib.parse.urlparse(response.url).path == '/admin/', 'Admin rights missing'
    assert response.status == 200
print('PASS: Codespaces HTTP health, CSRF-protected admin/admin login, Tickets sidebar and Django admin access.')
