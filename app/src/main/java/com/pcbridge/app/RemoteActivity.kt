package com.pcbridge.app

import android.annotation.SuppressLint
import android.app.Activity
import android.app.DownloadManager
import android.content.Intent
import android.graphics.Color
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.view.WindowManager
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast

/** Shows the PC (served by pc_server.py) full-screen. */
class RemoteActivity : Activity() {
    private lateinit var web: WebView
    private var pick: ValueCallback<Array<Uri>>? = null

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        window.statusBarColor = MainActivity.BG
        window.navigationBarColor = Color.parseColor("#171A21")

        web = WebView(this).apply {
            setBackgroundColor(MainActivity.BG)
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            settings.builtInZoomControls = true
            settings.displayZoomControls = false
            webViewClient = object : WebViewClient() {
                override fun onReceivedError(v: WebView, req: android.webkit.WebResourceRequest, err: android.webkit.WebResourceError) {
                    if (req.isForMainFrame) {
                        Toast.makeText(this@RemoteActivity, "PC disconnected", Toast.LENGTH_SHORT).show(); finish()
                    }
                }
            }
            webChromeClient = object : WebChromeClient() {
                override fun onShowFileChooser(v: WebView, cb: ValueCallback<Array<Uri>>, p: FileChooserParams): Boolean {
                    pick?.onReceiveValue(null)
                    pick = cb
                    val i = Intent(Intent.ACTION_GET_CONTENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
                        .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true)
                    startActivityForResult(Intent.createChooser(i, "Send to PC"), 1)
                    return true
                }
            }
            // "Save to phone" -> Downloads folder
            setDownloadListener { url, _, disposition, mime, _ ->
                val name = URLUtil.guessFileName(url, disposition, mime)
                val req = DownloadManager.Request(Uri.parse(url))
                    .setTitle(name)
                    .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                    .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
                (getSystemService(DOWNLOAD_SERVICE) as DownloadManager).enqueue(req)
                Toast.makeText(this@RemoteActivity, "Saving $name to Downloads", Toast.LENGTH_SHORT).show()
            }
        }
        setContentView(web)
        web.loadUrl(intent.getStringExtra("url")!!)
    }

    @Deprecated("Deprecated in Java")
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != 1) return
        val uris = when {
            resultCode != RESULT_OK || data == null -> null
            data.clipData != null -> data.clipData!!.let { c -> Array(c.itemCount) { c.getItemAt(it).uri } }
            data.data != null -> arrayOf(data.data!!)
            else -> null
        }
        pick?.onReceiveValue(uris)
        pick = null
    }

    override fun onPause() { super.onPause(); web.onPause() }   // stops screen stream in background
    override fun onResume() { super.onResume(); web.onResume() }
    override fun onDestroy() { web.destroy(); super.onDestroy() }

    @Deprecated("Deprecated in Java")
    override fun onBackPressed() {
        if (web.canGoBack()) web.goBack() else super.onBackPressed()
    }
}
