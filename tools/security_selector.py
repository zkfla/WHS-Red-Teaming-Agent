from pathlib import Path


SECURITY_KEYWORDS = {
    "WEBVIEW": [
        "WebView",
        "setJavaScriptEnabled",
        "addJavascriptInterface",
        "loadUrl"
    ],

    "STORAGE": [
        "SharedPreferences",
        "SQLiteDatabase",
        "FileOutputStream",
        "getExternalStorage"
    ],

    "CRYPTO": [
        "Cipher",
        "MessageDigest",
        "SecretKey",
        "AES",
        "DES",
        "MD5",
        "SHA1"
    ],

    "NETWORK": [
        "HttpURLConnection",
        "HttpsURLConnection",
        "TrustManager",
        "HostnameVerifier",
        "onReceivedSslError"
    ],

    "ANDROID_COMPONENT": [
        "Intent",
        "BroadcastReceiver",
        "ContentProvider"
    ],

    "LOGGING": [
        "Log.d",
        "Log.i",
        "Log.e"
    ]
}


def select_security_files(
    java_files: list[str]
):

    candidates = []

    for file_path in java_files:

        path = Path(file_path)

        try:
            text = path.read_text(
                encoding="utf-8",
                errors="ignore"
            )

        except Exception:
            continue

        matched_categories = []

        for category, keywords \
                in SECURITY_KEYWORDS.items():

            if any(
                keyword in text
                for keyword in keywords
            ):
                matched_categories.append(
                    category
                )

        if matched_categories:

            candidates.append({
                "file": str(path),
                "categories":
                    matched_categories
            })

    return candidates