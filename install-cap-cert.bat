@echo off
REM One-time setup on each staff PC: trust the CAP dashboard's HTTPS certificate.
REM
REM The dashboard (https://cap.lan:8421) uses a certificate signed by the firm's
REM own "Collier Law CAP Root CA" (see src/cap_certs.py) — Clio sign-in requires
REM HTTPS. Until a PC trusts that root CA, the browser shows "Your connection is
REM not private". This downloads the root CA's public certificate from the
REM dashboard and adds it to the current user's Trusted Root store (no admin
REM rights needed; Windows shows one "install this certificate?" prompt — click
REM Yes). Chrome and Edge use this store. Firefox has its own and isn't covered.
REM
REM -k is deliberate: this PC can't verify the dashboard yet — that's the point.
REM The root CA cert is public (no private key), so fetching it unverified is safe;
REM check the thumbprint shown below against the one Ted gives you if in doubt.

set CERTFILE=%TEMP%\cap-root-ca.crt
echo Downloading the CAP root certificate from cap.lan...
curl.exe -k -s -f -o "%CERTFILE%" https://cap.lan:8421/cap-root-ca.crt
if errorlevel 1 (
    echo ERROR: couldn't reach https://cap.lan:8421 -- is this PC on the office network/Tailscale?
    pause
    exit /b 1
)

certutil -user -addstore Root "%CERTFILE%"
if errorlevel 1 (
    echo ERROR: the certificate wasn't installed ^(was the Windows prompt declined?^)
    pause
    exit /b 1
)

del "%CERTFILE%"
echo.
echo Done. Close and reopen your browser, then go to https://cap.lan:8421/
pause
