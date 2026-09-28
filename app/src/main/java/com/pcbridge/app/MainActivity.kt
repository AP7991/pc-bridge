package com.pcbridge.app

import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var store: Store
    private lateinit var list: LinearLayout
    private lateinit var status: TextView
    @Volatile private var job = 0  // bumps to cancel a running connect

    override fun onCreate(saved: Bundle?) {
        super.onCreate(saved)
        store = Store(this)
        window.statusBarColor = BG

        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding(dp(20), dp(28), dp(20), dp(20)) }
        root.addView(text("PC Bridge", 28f, Color.WHITE))
        root.addView(text("Your PC, on your phone.", 15f, MUTED).apply { setPadding(0, dp(4), 0, dp(20)) })
        status = text("", 15f, ACCENT).apply { visibility = View.GONE; setPadding(0, 0, 0, dp(16)) }
        root.addView(status)
        list = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(list)
        root.addView(button("Find my PC", primary = true) { find() })
        root.addView(button("Add by IP address") { askIp() })

        setContentView(ScrollView(this).apply { setBackgroundColor(BG); addView(root) })
        render()

        if (saved == null) store.all().firstOrNull { it.name == store.last }?.let { connect(it) }
    }

    override fun onResume() { super.onResume(); render() }
    override fun onPause() { super.onPause(); job++ }

    // ---------- UI ----------
    private fun render() {
        list.removeAllViews()
        for (pc in store.all()) {
            val card = LinearLayout(this).apply {
                orientation = LinearLayout.VERTICAL
                setPadding(dp(16), dp(14), dp(16), dp(14))
                background = GradientDrawable().apply { setColor(CARD); cornerRadius = dp(16).toFloat() }
                layoutParams = LinearLayout.LayoutParams(-1, -2).apply { bottomMargin = dp(12) }
                setOnLongClickListener { confirmRemove(pc); true }
            }
            card.addView(text(pc.name, 18f, Color.WHITE))
            card.addView(text(pc.ip, 13f, MUTED))
            val row = LinearLayout(this).apply { setPadding(0, dp(10), 0, 0) }
            row.addView(button("Open", primary = true, weight = true) { connect(pc) })
            row.addView(View(this), LinearLayout.LayoutParams(dp(10), 1))
            row.addView(button("Turn on", weight = true) {
                thread { Net.wake(this, pc) }; toast("Wake signal sent to ${pc.name}")
            })
            card.addView(row)
            list.addView(card)
        }
    }

    private fun setStatus(s: String?) = runOnUiThread {
        status.text = s ?: ""; status.visibility = if (s == null) View.GONE else View.VISIBLE
    }

    // ---------- connect (wakes the PC if needed) ----------
    private fun connect(pc: Pc) {
        val my = ++job
        store.last = pc.name
        setStatus("Connecting to ${pc.name}...")
        thread {
            fun alive() = my == job
            fun open(p: Pc) = runOnUiThread {
                if (!alive()) return@runOnUiThread
                store.upsert(p); setStatus(null)
                startActivity(Intent(this, RemoteActivity::class.java).putExtra("url", p.url))
            }
            if (Net.reachable(pc.ip, pc.port)) return@thread open(pc)
            relocate(pc, 1500)?.let { return@thread open(it) }   // PC got a new IP?

            setStatus("Turning on ${pc.name}...  (tap here to cancel)")
            runOnUiThread { status.setOnClickListener { job++; setStatus(null) } }
            val end = System.currentTimeMillis() + 120_000
            var nextWake = 0L
            while (alive() && System.currentTimeMillis() < end) {
                if (System.currentTimeMillis() >= nextWake) { Net.wake(this, pc); nextWake = System.currentTimeMillis() + 15_000 }
                if (Net.reachable(pc.ip, pc.port, 1000)) return@thread open(pc)
                relocate(pc, 1500)?.let { return@thread open(it) }
                Thread.sleep(1500)
            }
            if (alive()) setStatus("Couldn't reach ${pc.name}. Check it's plugged in, on the same Wi-Fi/router, and PC Bridge is set up.")
        }
    }

    private fun relocate(pc: Pc, ms: Int): Pc? =
        Net.discover(this, ms).firstOrNull { it.name == pc.name }?.let { pc.copy(ip = it.ip, macs = it.macs) }

    // ---------- add a PC ----------
    private fun find() {
        setStatus("Looking for your PC on Wi-Fi...")
        thread {
            val found = Net.discover(this)
            runOnUiThread {
                setStatus(null)
                when {
                    found.isEmpty() -> AlertDialog.Builder(this).setTitle("No PC found")
                        .setMessage("Make sure:\n• phone and PC are on the same Wi-Fi\n• you ran setup.bat on the PC")
                        .setPositiveButton("Try again") { _, _ -> find() }
                        .setNeutralButton("Enter IP") { _, _ -> askIp() }.show()
                    found.size == 1 -> askPin(found[0])
                    else -> AlertDialog.Builder(this).setTitle("Pick your PC")
                        .setItems(found.map { "${it.name}  (${it.ip})" }.toTypedArray()) { _, i -> askPin(found[i]) }.show()
                }
            }
        }
    }

    private fun askIp() {
        val ip = EditText(this).apply { hint = "192.168.1.20"; inputType = InputType.TYPE_CLASS_PHONE }
        AlertDialog.Builder(this).setTitle("PC's IP address").setView(pad(ip))
            .setPositiveButton("Next") { _, _ ->
                val addr = ip.text.toString().trim()
                thread {
                    val info = runCatching {
                        val c = java.net.URL("http://$addr:47801/api/ping").openConnection() as java.net.HttpURLConnection
                        c.connectTimeout = 3000; c.responseCode  // 401 = PC Bridge is there
                    }.getOrNull()
                    runOnUiThread {
                        if (info == 401) askPin(Net.Found(addr, addr, 47801, emptyList()))
                        else toast("No PC Bridge at $addr")
                    }
                }
            }.show()
    }

    private fun askPin(f: Net.Found) {
        val pin = EditText(this).apply { hint = "8-digit PIN shown by setup.bat"; inputType = InputType.TYPE_CLASS_NUMBER }
        AlertDialog.Builder(this).setTitle("Connect to ${f.name}").setView(pad(pin))
            .setPositiveButton("Connect") { _, _ ->
                val p = pin.text.toString().trim()
                thread {
                    val ok = Net.checkPin(f.ip, f.port, p)
                    var macs = f.macs
                    var name = f.name
                    if (ok && macs.isEmpty()) runCatching {   // added by IP: fetch name + MAC for Wake-on-LAN
                        val o = org.json.JSONObject(java.net.URL("http://${f.ip}:${f.port}/api/ping?t=$p").readText())
                        name = o.getString("name")
                        macs = o.getJSONArray("macs").let { a -> List(a.length()) { a.getString(it) } }
                    }
                    runOnUiThread {
                        if (!ok) return@runOnUiThread toast("Wrong PIN")
                        val pc = Pc(name, f.ip, f.port, p, macs)
                        store.upsert(pc); render(); connect(pc)
                    }
                }
            }.show()
    }

    private fun confirmRemove(pc: Pc) {
        AlertDialog.Builder(this).setTitle("Remove ${pc.name}?")
            .setPositiveButton("Remove") { _, _ -> store.remove(pc); render() }
            .setNegativeButton("Cancel", null).show()
    }

    // ---------- tiny view helpers ----------
    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
    private fun toast(s: String) = Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
    private fun pad(v: View) = LinearLayout(this).apply { setPadding(dp(20), dp(8), dp(20), 0); addView(v, -1, -2) }
    private fun text(s: String, size: Float, color: Int) = TextView(this).apply { text = s; textSize = size; setTextColor(color) }
    private fun button(label: String, primary: Boolean = false, weight: Boolean = false, onClick: () -> Unit) =
        Button(this).apply {
            text = label; isAllCaps = false; textSize = 16f; gravity = Gravity.CENTER
            setTextColor(if (primary) Color.WHITE else ACCENT)
            background = GradientDrawable().apply {
                setColor(if (primary) BLUE else CARD2); cornerRadius = dp(12).toFloat()
            }
            layoutParams = if (weight) LinearLayout.LayoutParams(0, dp(48), 1f)
            else LinearLayout.LayoutParams(-1, dp(52)).apply { topMargin = dp(10) }
            setOnClickListener { onClick() }
        }

    companion object {
        val BG = Color.parseColor("#0F1115")
        val CARD = Color.parseColor("#171A21")
        val CARD2 = Color.parseColor("#222733")
        val BLUE = Color.parseColor("#2F6FDE")
        val ACCENT = Color.parseColor("#6EA8FF")
        val MUTED = Color.parseColor("#8A8F9C")
    }
}
