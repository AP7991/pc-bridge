package com.pcbridge.app

import android.content.Context
import android.net.wifi.WifiManager
import org.json.JSONArray
import org.json.JSONObject
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.HttpURLConnection
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.Socket
import java.net.SocketTimeoutException
import java.net.URL

data class Pc(val name: String, var ip: String, val port: Int, val pin: String, var macs: List<String>) {
    val url get() = "http://$ip:$port/?t=$pin"

    fun toJson() = JSONObject().put("name", name).put("ip", ip).put("port", port).put("pin", pin)
        .put("macs", JSONArray(macs))

    companion object {
        fun fromJson(o: JSONObject) = Pc(
            o.getString("name"), o.getString("ip"), o.getInt("port"), o.optString("pin"),
            o.getJSONArray("macs").let { a -> List(a.length()) { a.getString(it) } })
    }
}

/** Saved PCs, kept in SharedPreferences. */
class Store(ctx: Context) {
    private val prefs = ctx.getSharedPreferences("pcs", Context.MODE_PRIVATE)

    fun all(): MutableList<Pc> {
        val a = JSONArray(prefs.getString("list", "[]"))
        return MutableList(a.length()) { Pc.fromJson(a.getJSONObject(it)) }
    }

    fun save(list: List<Pc>) = prefs.edit().putString("list", JSONArray(list.map { it.toJson() }).toString()).apply()

    fun upsert(pc: Pc) = save(all().filter { it.name != pc.name } + pc)
    fun remove(pc: Pc) = save(all().filter { it.name != pc.name })

    var last: String?
        get() = prefs.getString("last", null)
        set(v) = prefs.edit().putString("last", v).apply()
}

object Net {
    const val DISCOVERY_PORT = 47800

    data class Found(val name: String, val ip: String, val port: Int, val macs: List<String>)

    /** Shouts on Wi-Fi "any PC Bridge PCs here?" and collects replies. */
    fun discover(ctx: Context, waitMs: Int = 2500): List<Found> {
        val wifi = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val lock = wifi.createMulticastLock("pcbridge").apply { setReferenceCounted(false); acquire() }
        val found = linkedMapOf<String, Found>()
        try {
            DatagramSocket().use { s ->
                s.broadcast = true
                s.soTimeout = 400
                val msg = "PCBRIDGE?".toByteArray()
                val targets = listOf(InetAddress.getByName("255.255.255.255")) + listOfNotNull(subnetBroadcast(ctx))
                val end = System.currentTimeMillis() + waitMs
                var nextSend = 0L
                val buf = ByteArray(2048)
                while (System.currentTimeMillis() < end) {
                    if (System.currentTimeMillis() >= nextSend) {
                        targets.forEach { runCatching { s.send(DatagramPacket(msg, msg.size, it, DISCOVERY_PORT)) } }
                        nextSend = System.currentTimeMillis() + 800
                    }
                    try {
                        val p = DatagramPacket(buf, buf.size)
                        s.receive(p)
                        val o = JSONObject(String(p.data, 0, p.length))
                        val macs = o.getJSONArray("macs").let { a -> List(a.length()) { a.getString(it) } }
                        found[o.getString("name")] = Found(o.getString("name"), p.address.hostAddress!!, o.getInt("port"), macs)
                    } catch (_: SocketTimeoutException) {
                    }
                }
            }
        } catch (_: Exception) {
        } finally {
            lock.release()
        }
        return found.values.toList()
    }

    @Suppress("DEPRECATION")
    private fun subnetBroadcast(ctx: Context): InetAddress? {
        val wifi = ctx.applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val d = wifi.dhcpInfo ?: return null
        if (d.ipAddress == 0) return null
        val b = (d.ipAddress and d.netmask) or d.netmask.inv()
        return InetAddress.getByAddress(ByteArray(4) { ((b shr (it * 8)) and 0xFF).toByte() })
    }

    /** Wake-on-LAN: the "magic packet" that switches a sleeping/off PC on. */
    fun wake(ctx: Context, pc: Pc) {
        val targets = mutableListOf(InetAddress.getByName("255.255.255.255"))
        subnetBroadcast(ctx)?.let { targets += it }
        runCatching { targets += InetAddress.getByName(pc.ip.substringBeforeLast('.') + ".255") }
        DatagramSocket().use { s ->
            s.broadcast = true
            for (mac in pc.macs) {
                val m = mac.split(':', '-').map { it.toInt(16).toByte() }
                if (m.size != 6) continue
                val pkt = ByteArray(6) { 0xFF.toByte() } + List(16) { m }.flatten()
                repeat(3) {
                    for (t in targets) for (port in intArrayOf(9, 7)) {
                        runCatching { s.send(DatagramPacket(pkt, pkt.size, t, port)) }
                    }
                }
            }
        }
    }

    fun reachable(ip: String, port: Int, timeout: Int = 1500) = try {
        Socket().use { it.connect(InetSocketAddress(ip, port), timeout) }; true
    } catch (_: Exception) {
        false
    }

    /** true = PIN correct. */
    fun checkPin(ip: String, port: Int, pin: String): Boolean = try {
        val c = URL("http://$ip:$port/api/ping?t=$pin").openConnection() as HttpURLConnection
        c.connectTimeout = 3000; c.readTimeout = 3000
        c.responseCode == 200
    } catch (_: Exception) {
        false
    }
}
