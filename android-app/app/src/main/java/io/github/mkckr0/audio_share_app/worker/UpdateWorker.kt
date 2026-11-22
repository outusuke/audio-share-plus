/*
 *    Copyright 2022-2024 mkckr0 <https://github.com/mkckr0>
 *
 *    Licensed under the Apache License, Version 2.0 (the "License");
 *    you may not use this file except in compliance with the License.
 *    You may obtain a copy of the License at
 *
 *        http://www.apache.org/licenses/LICENSE-2.0
 *
 *    Unless required by applicable law or agreed to in writing, software
 *    distributed under the License is distributed on an "AS IS" BASIS,
 *    WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 *    See the License for the specific language governing permissions and
 *    limitations under the License.
 */

package io.github.mkckr0.audio_share_app.worker

import android.Manifest
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.widget.Toast
import androidx.core.app.ActivityCompat
import androidx.core.app.NotificationCompat
import androidx.core.app.NotificationManagerCompat
import androidx.work.Constraints
import androidx.work.CoroutineWorker
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import androidx.work.WorkerParameters
import androidx.work.workDataOf
import io.github.mkckr0.audio_share_app.BuildConfig
import io.github.mkckr0.audio_share_app.R
import io.github.mkckr0.audio_share_app.model.Channel
import io.github.mkckr0.audio_share_app.model.LatestRelease
import io.github.mkckr0.audio_share_app.model.Notification
import io.github.mkckr0.audio_share_app.model.Util
import io.github.mkckr0.audio_share_app.model.WorkName
import io.github.mkckr0.audio_share_app.ui.MainActivity
import io.ktor.client.HttpClient
import io.ktor.client.call.body
import io.ktor.client.plugins.contentnegotiation.ContentNegotiation
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.serialization.kotlinx.json.json
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.ExperimentalSerializationApi
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNamingStrategy
import java.util.concurrent.TimeUnit

class UpdateWorker(appContext: Context, workerParams: WorkerParameters) :
    CoroutineWorker(appContext, workerParams) {
    private val tag = javaClass.simpleName

    private val suppressMessage get() = inputData.getBoolean(SUPPRESS_MESSAGE, false)

    @OptIn(ExperimentalSerializationApi::class)
    override suspend fun doWork(): Result {

        val httpClient = HttpClient {
            expectSuccess = true
            install(ContentNegotiation) {
                json(Json {
                    ignoreUnknownKeys = true
                    isLenient = true
                    namingStrategy = JsonNamingStrategy.SnakeCase
                })
            }
        }

        val latestRelease: LatestRelease = try {
            httpClient.use {
                it.get("https://api.github.com/repos/outusuke/audio-share-plus/releases/latest") {
                    header("Accept", "application/vnd.github+json")
                    header("User-Agent", "audio-share-plus/${BuildConfig.VERSION_NAME}")
                }.body<LatestRelease>()
            }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // 404 when no release is published yet
            Log.w(tag, "update check failed", e)
            showMessage(applicationContext.getString(R.string.label_update_check_failed))
            return if (isPeriodic()) Result.success() else Result.failure()
        }

        val isNewer = try {
            Util.isNewerVersion(latestRelease.tagName, "v${BuildConfig.VERSION_NAME}")
        } catch (e: IllegalArgumentException) {
            Log.w(tag, "unexpected release tag: ${latestRelease.tagName} / ${BuildConfig.VERSION_NAME}", e)
            false
        }
        applicationContext.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putLong(KEY_LAST_CHECK, System.currentTimeMillis()).apply()
        Log.i(tag, "latest=${latestRelease.tagName} installed=v${BuildConfig.VERSION_NAME} newer=$isNewer assets=${latestRelease.assets.map { it.name }}")
        if (!isNewer) {
            showMessage(applicationContext.getString(R.string.label_no_update))
            return Result.success()
        }

        val apkAsset = latestRelease.assets.find {
            it.name.matches(Regex("audio-share-app-[0-9.]*-release.apk"))
        }
        if (apkAsset == null) {
            showMessage(applicationContext.getString(R.string.label_has_an_update_1))
            return Result.success()
        }

        if (ActivityCompat.checkSelfPermission(
                applicationContext, Manifest.permission.POST_NOTIFICATIONS
            ) != PackageManager.PERMISSION_GRANTED
        ) {
            showMessage(applicationContext.getString(R.string.label_no_post_notification_permission))
            return Result.success()
        }

        val intent = Intent(applicationContext, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_NEW_TASK
            putExtra("action", "update")
            putExtra("apkAsset", apkAsset)
        }
        val pendingIntent = PendingIntent.getActivity(
            applicationContext, 0, intent,
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )

        val notification = NotificationCompat.Builder(applicationContext, Channel.UPDATE.id)
            .setSmallIcon(R.drawable.baseline_update)
            .setContentTitle(
                applicationContext.getString(R.string.label_has_an_update_2).format(latestRelease.tagName)
            )
            .setContentText(applicationContext.getString(R.string.label_tap_notification_1))
            .setContentIntent(pendingIntent)
            .setAutoCancel(true)
            .setOnlyAlertOnce(true)
            .build()

        NotificationManagerCompat.from(applicationContext).notify(Notification.UPDATE.id, notification)
        showMessage(applicationContext.getString(R.string.label_tap_notification_2))
        return Result.success()
    }

    private fun isPeriodic() = tags.contains(TAG_PERIODIC)

    private fun showMessage(message: String) {
        if (suppressMessage) return
        Handler(Looper.getMainLooper()).post {
            Toast.makeText(applicationContext, message, Toast.LENGTH_SHORT).show()
        }
    }

    companion object {
        const val SUPPRESS_MESSAGE = "SUPPRESS_MESSAGE"
        const val TAG_PERIODIC = "update_periodic"
        private const val PREFS = "update_worker"
        private const val KEY_LAST_CHECK = "last_check_ms"
        private const val LAUNCH_CHECK_INTERVAL_MS = 6 * 60 * 60 * 1000L

        fun schedulePeriodic(context: Context, replace: Boolean = false) {
            val request = PeriodicWorkRequestBuilder<UpdateWorker>(
                3, TimeUnit.HOURS,
                5, TimeUnit.MINUTES,
            )
                .setConstraints(
                    Constraints.Builder().setRequiredNetworkType(NetworkType.CONNECTED).build()
                )
                .addTag(TAG_PERIODIC)
                .setInputData(workDataOf(SUPPRESS_MESSAGE to true))
                .build()
            WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                WorkName.AUTO_CHECK_UPDATE.value,
                if (replace) ExistingPeriodicWorkPolicy.CANCEL_AND_REENQUEUE else ExistingPeriodicWorkPolicy.KEEP,
                request
            )
        }

        fun checkOnLaunch(context: Context) {
            val last = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getLong(KEY_LAST_CHECK, 0)
            if (System.currentTimeMillis() - last >= LAUNCH_CHECK_INTERVAL_MS) checkNow(context)
        }

        fun checkNow(context: Context) {
            val request = OneTimeWorkRequestBuilder<UpdateWorker>()
                .setInputData(workDataOf(SUPPRESS_MESSAGE to true))
                .build()
            WorkManager.getInstance(context).enqueueUniqueWork(
                WorkName.CHECK_UPDATE.value, ExistingWorkPolicy.REPLACE, request
            )
        }

        fun cancelPeriodic(context: Context) {
            WorkManager.getInstance(context).cancelUniqueWork(WorkName.AUTO_CHECK_UPDATE.value)
        }
    }
}
