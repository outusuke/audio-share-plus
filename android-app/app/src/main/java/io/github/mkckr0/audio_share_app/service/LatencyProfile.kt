package io.github.mkckr0.audio_share_app.service

enum class LatencyProfile(
    val prebufferMs: Int,
    val minQueueMs: Int,
    val maxQueueMs: Int,
    val holdWifiLock: Boolean,
) {
    LOW(20, 20, 100, true),
    BALANCED(60, 40, 200, true),
    // the deep queue absorbs Wi-Fi power save bursts
    STABLE(150, 100, 400, false);

    companion object {
        fun fromIndex(index: Int): LatencyProfile = values().getOrElse(index) { BALANCED }
    }
}
