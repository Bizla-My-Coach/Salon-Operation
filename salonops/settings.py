from pathlib import Path
import os
from django.core.exceptions import ImproperlyConfigured


def env_bool(name, default=False):
    value = os.getenv(name, str(default)).strip().lower()
    if value not in ('true', 'false', '1', '0'):
        raise ImproperlyConfigured(f'{name} must be true or false.')
    return value in ('true', '1')


def required_env(name):
    value = os.getenv(name, '')
    if not value.strip():
        raise ImproperlyConfigured(f'{name} is required. Configure PostgreSQL and secrets in .env; SQLite is not supported.')
    return value

BASE_DIR=Path(__file__).resolve().parent.parent
DEBUG=env_bool('DEBUG', True)
APP_ENV=os.getenv('APP_ENV', 'development').strip().lower()
if APP_ENV == 'production' and DEBUG:
    raise ImproperlyConfigured('Production requires DEBUG=False.')
SECRET_KEY=required_env('SECRET_KEY')
if not DEBUG and (len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 5 or SECRET_KEY.startswith(('dev-only', 'django-insecure-', 'replace-'))):
    raise ImproperlyConfigured('Use a unique random SECRET_KEY of at least 50 characters for HTTPS deployments.')
ALLOWED_HOSTS=[x.strip() for x in os.getenv('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if x.strip()]
if not DEBUG and (not ALLOWED_HOSTS or '*' in ALLOWED_HOSTS):
    raise ImproperlyConfigured('Set explicit ALLOWED_HOSTS for HTTPS deployments; wildcard hosts are not allowed.')
CSRF_TRUSTED_ORIGINS=[x.strip() for x in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if x.strip()]
if not DEBUG and any(not origin.startswith('https://') or '*' in origin for origin in CSRF_TRUSTED_ORIGINS):
    raise ImproperlyConfigured('CSRF_TRUSTED_ORIGINS must contain explicit https:// origins.')
SECURE_SSL_REDIRECT=env_bool('SECURE_SSL_REDIRECT', not DEBUG)
SESSION_COOKIE_SECURE=env_bool('SESSION_COOKIE_SECURE', not DEBUG)
CSRF_COOKIE_SECURE=env_bool('CSRF_COOKIE_SECURE', not DEBUG)
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE='Lax'
CSRF_COOKIE_SAMESITE='Lax'
SECURE_HSTS_SECONDS=int(os.getenv('SECURE_HSTS_SECONDS', '300' if not DEBUG else '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS=env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', False)
SECURE_HSTS_PRELOAD=env_bool('SECURE_HSTS_PRELOAD', False)
# Only enable behind a proxy that overwrites this header; keep the backend private.
TRUST_PROXY_HEADERS=env_bool('TRUST_PROXY_HEADERS', False)
SECURE_PROXY_SSL_HEADER=('HTTP_X_FORWARDED_PROTO', 'https') if TRUST_PROXY_HEADERS else None
# Private container health checks use HTTP and expose only database availability.
SECURE_REDIRECT_EXEMPT=[r'^health/$']
if APP_ENV == 'production' and not all((SECURE_SSL_REDIRECT, SESSION_COOKIE_SECURE, CSRF_COOKIE_SECURE)):
    raise ImproperlyConfigured('Production requires HTTPS redirects and secure session/CSRF cookies.')
if APP_ENV == 'private-preview' and (
    DEBUG or set(ALLOWED_HOSTS) != {'localhost', '127.0.0.1'} or CSRF_TRUSTED_ORIGINS
    or TRUST_PROXY_HEADERS or SECURE_SSL_REDIRECT or SESSION_COOKIE_SECURE
    or CSRF_COOKIE_SECURE or SECURE_HSTS_SECONDS != 0
    or SECURE_HSTS_INCLUDE_SUBDOMAINS or SECURE_HSTS_PRELOAD
):
    raise ImproperlyConfigured('Private preview requires DEBUG=False, localhost-only hosts and SSH-tunnel HTTP settings.')
INSTALLED_APPS=['django.contrib.admin','django.contrib.auth','django.contrib.contenttypes','django.contrib.sessions','django.contrib.messages','django.contrib.staticfiles','operations']
MIDDLEWARE=['django.middleware.security.SecurityMiddleware','whitenoise.middleware.WhiteNoiseMiddleware','django.contrib.sessions.middleware.SessionMiddleware','django.middleware.common.CommonMiddleware','operations.upload_limits.WorkbookUploadLimitMiddleware','django.middleware.csrf.CsrfViewMiddleware','django.contrib.auth.middleware.AuthenticationMiddleware','django.contrib.messages.middleware.MessageMiddleware','django.middleware.clickjacking.XFrameOptionsMiddleware']
ROOT_URLCONF='salonops.urls'
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[BASE_DIR/'templates'],'APP_DIRS':True,'OPTIONS':{'context_processors':['django.template.context_processors.request','django.contrib.auth.context_processors.auth','django.contrib.messages.context_processors.messages','operations.context_processors.acting_branch','operations.review_notifications.review_notifications']}}]
WSGI_APPLICATION='salonops.wsgi.application'
ASGI_APPLICATION='salonops.asgi.application'
DATABASES={'default':{
        'ENGINE':'django.db.backends.postgresql',
        'NAME':required_env('POSTGRES_DB'),
        'USER':required_env('POSTGRES_USER'),
        'PASSWORD':required_env('POSTGRES_PASSWORD'),
        'HOST':required_env('POSTGRES_HOST'),
        'PORT':os.getenv('POSTGRES_PORT','5432'),
        'CONN_MAX_AGE':60,
    }}
AUTH_PASSWORD_VALIDATORS=[
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator'},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]
# Additional senior approval is optional; sensitivity alone does not block manager closure.
REVIEW_SENSITIVE_CLOSURE_SENIOR_APPROVAL=env_bool('REVIEW_SENSITIVE_CLOSURE_SENIOR_APPROVAL', False)
REVIEW_SENSITIVE_FOLLOWUP_SENIOR_APPROVAL=env_bool('REVIEW_SENSITIVE_FOLLOWUP_SENIOR_APPROVAL', False)
# Dedicated receive-only AWS API credentials. No session or demo-password fallback.
REVIEW_API_TOKENS={env: os.getenv('REVIEW_INTEGRATION_KEY_' + env.upper(), '') or
    os.getenv('REVIEW_API_TOKEN_' + env.upper(), '') for env in ('production', 'sandbox')}
REVIEW_API_REQUIRE_HTTPS=env_bool('REVIEW_API_REQUIRE_HTTPS', not DEBUG)
LANGUAGE_CODE='en-us'; TIME_ZONE=os.getenv('TIME_ZONE','Asia/Kolkata'); USE_I18N=True; USE_TZ=True
STATIC_URL='static/'; STATIC_ROOT=BASE_DIR/'staticfiles'; STATICFILES_DIRS=[BASE_DIR/'static']
STORAGES={'default':{'BACKEND':'django.core.files.storage.FileSystemStorage'},'staticfiles':{'BACKEND':'whitenoise.storage.CompressedManifestStaticFilesStorage'}}
DEFAULT_AUTO_FIELD='django.db.models.BigAutoField'
LOGIN_URL='login'; LOGIN_REDIRECT_URL='dashboard'; LOGOUT_REDIRECT_URL='login'
