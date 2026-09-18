package qimei;

import java.io.IOException;
import java.io.InputStream;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.time.Duration;

/**
 * Resolve a verified com.tencent.qt.qtl 12.8.1 APK: reuse QIMEI_APK_PATH when set,
 * otherwise download the pinned build once and cache it. Every candidate is checked
 * against the pinned size + SHA-256 + MD5 before use.
 */
final class ApkResolver {
    static final String VERSION = "12.8.1";
    static final long SIZE = 143326645L;
    static final String SHA256 = "4eee225a12905bbff95924bef3511668322533adf04f65aaec4cf86048f946d8";
    static final String MD5 = "7109ebd8f88abd4775dac1aff48d96dd";
    static final String FILENAME = "com.tencent.qt.qtl_" + VERSION + ".apk";

    // Default Wandoujia/pp.cn mirror serving the exact 143326645-byte build; the
    // front host 302-redirects to a CDN, so redirects must be followed. Override
    // with QIMEI_APK_DOWNLOAD_URL (a different mirror must still pass verify()).
    private static final String DEFAULT_DOWNLOAD_URL =
            "https://android-apps.pp.cn/fs08/2026/08/28/0/"
                    + "106_7af413265f4c9746e5d365f9094ee4f3.apk"
                    + "?yingid=web_space&packageid=207502909&md5=7109ebd8f88abd4775dac1aff48d96dd"
                    + "&minSDK=21&size=143326645&shortMd5=1426917cf0a319b16d845ca2e409b147"
                    + "&crc32=1453447322&did=282ec81b2379d4e120776d1b460e2ad5&nrd=0";

    private ApkResolver() {
    }

    private static String downloadUrl() {
        String override = System.getenv("QIMEI_APK_DOWNLOAD_URL");
        return (override != null && !override.isBlank()) ? override : DEFAULT_DOWNLOAD_URL;
    }

    /** Return a verified APK, downloading into {@code dataDir} when needed. */
    static Path resolve(Path dataDir) throws IOException, InterruptedException {
        String override = System.getenv("QIMEI_APK_PATH");
        if (override != null && !override.isBlank()) {
            Path apk = Path.of(override);
            if (!Files.isRegularFile(apk)) {
                throw new IOException("QIMEI_APK_PATH does not exist: " + apk);
            }
            if (!verify(apk)) {
                throw new IOException("QIMEI_APK_PATH failed integrity check (expects 12.8.1): " + apk);
            }
            return apk;
        }

        Files.createDirectories(dataDir);
        Path apk = dataDir.resolve(FILENAME);
        if (Files.isRegularFile(apk) && verify(apk)) {
            return apk;
        }

        System.err.printf("downloading %s (%d bytes) for one-time QIMEI bootstrap%n", FILENAME, SIZE);
        Path temporary = Files.createTempFile(dataDir, "." + FILENAME + ".", ".part");
        try {
            HttpClient client = HttpClient.newBuilder()
                    .connectTimeout(Duration.ofSeconds(30))
                    .followRedirects(HttpClient.Redirect.ALWAYS)
                    .build();
            HttpRequest request = HttpRequest.newBuilder(URI.create(downloadUrl()))
                    .timeout(Duration.ofMinutes(5))
                    .GET()
                    .build();
            HttpResponse<InputStream> response =
                    client.send(request, HttpResponse.BodyHandlers.ofInputStream());
            if (response.statusCode() != 200) {
                throw new IOException("APK download failed with status " + response.statusCode());
            }
            try (InputStream body = response.body()) {
                Files.copy(body, temporary, StandardCopyOption.REPLACE_EXISTING);
            }
            if (!verify(temporary)) {
                throw new IOException(
                        "Downloaded APK failed integrity check (size/SHA-256/MD5 mismatch); "
                                + "expected " + SIZE + " bytes, sha256 " + SHA256);
            }
            Files.move(temporary, apk, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
            System.err.println("APK verified and cached at " + apk);
            return apk;
        } finally {
            Files.deleteIfExists(temporary);
        }
    }

    static boolean verify(Path path) {
        try {
            if (Files.size(path) != SIZE) {
                return false;
            }
            MessageDigest sha = MessageDigest.getInstance("SHA-256");
            MessageDigest md5 = MessageDigest.getInstance("MD5");
            try (InputStream input = Files.newInputStream(path)) {
                byte[] buffer = new byte[1 << 20];
                int read;
                while ((read = input.read(buffer)) != -1) {
                    sha.update(buffer, 0, read);
                    md5.update(buffer, 0, read);
                }
            }
            return hex(sha.digest()).equals(SHA256) && hex(md5.digest()).equals(MD5);
        } catch (Exception exception) {
            return false;
        }
    }

    private static String hex(byte[] bytes) {
        StringBuilder builder = new StringBuilder();
        for (byte b : bytes) {
            builder.append(String.format("%02x", b));
        }
        return builder.toString();
    }
}
