package qimei;

import java.io.File;
import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.concurrent.Executors;
import java.util.regex.Pattern;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;

import org.json.JSONObject;

/**
 * Stateless HTTP front for QIMEI36 registration (JDK built-in HttpServer, no web deps).
 *
 * <p>The device fingerprint is owned by the caller (the lol plugin). This service
 * only runs the native library: it takes a native-config JSON body, emulates
 * libqimei.so under Unidbg, registers online, and returns the QIMEI36. It keeps no
 * device state and no QIMEI36 cache — those live on the caller side.
 *
 * <pre>
 *   GET  /healthz  -> {"status":"ok"}
 *   POST /qimei36  {"androidApi":..,"deviceInfo":{..},"sdkInfo":[..]} -> {"qimei36":"..."}
 * </pre>
 */
public final class QimeiServer {
    private static final Pattern QIMEI36 = Pattern.compile("^[0-9a-fA-F]{36}$");
    private static final int MAX_BODY = 1 << 20;

    private final Path dataDir = Path.of(envOrDefault("QIMEI_DATA_DIR", "/data"));
    private final Path libraryPath = Path.of(envOrDefault("QIMEI_SO_PATH", "lib/libqimei.so"));
    private final boolean debug = Boolean.parseBoolean(System.getenv("QIMEI_DEBUG"));

    private QimeiServer() {
    }

    public static void main(String[] args) throws IOException {
        int port = Integer.parseInt(envOrDefault("QIMEI_PORT", "8080"));
        QimeiServer service = new QimeiServer();
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);
        // Single-thread executor: the native run is heavy and must not race.
        server.setExecutor(Executors.newSingleThreadExecutor());
        server.createContext("/healthz", service::handleHealth);
        server.createContext("/qimei36", service::handleRegister);
        server.start();
        System.out.println("qimei service listening on :" + port);
    }

    private static String envOrDefault(String key, String fallback) {
        String value = System.getenv(key);
        return (value != null && !value.isBlank()) ? value : fallback;
    }

    private void handleHealth(HttpExchange exchange) throws IOException {
        respond(exchange, 200, new JSONObject().put("status", "ok"));
    }

    private void handleRegister(HttpExchange exchange) throws IOException {
        if (!"POST".equals(exchange.getRequestMethod())
                || !"/qimei36".equals(exchange.getRequestURI().getPath())) {
            respond(exchange, 404, new JSONObject().put("error", "not found"));
            return;
        }
        try {
            byte[] raw = exchange.getRequestBody().readNBytes(MAX_BODY);
            JSONObject nativeConfig = new JSONObject(new String(raw, StandardCharsets.UTF_8));
            String value = register(nativeConfig);
            respond(exchange, 200, new JSONObject().put("qimei36", value));
        } catch (Exception exception) {
            System.err.println("qimei register failed: " + exception);
            respond(exchange, 503, new JSONObject().put("error", String.valueOf(exception.getMessage())));
        }
    }

    // Run the native probe against the caller-supplied device config.
    private String register(JSONObject nativeConfig) throws Exception {
        Path apk = ApkResolver.resolve(dataDir);
        File library = libraryPath.toFile();
        if (!library.isFile()) {
            throw new IOException("libqimei.so not found: " + libraryPath);
        }
        String value = QimeiProbe.register(apk.toFile(), library, nativeConfig, debug);
        if (!QIMEI36.matcher(value).matches()) {
            throw new IllegalStateException("invalid QIMEI36 from native probe: " + value);
        }
        return value;
    }

    private void respond(HttpExchange exchange, int status, JSONObject body) throws IOException {
        byte[] bytes = body.toString().getBytes(StandardCharsets.UTF_8);
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream stream = exchange.getResponseBody()) {
            stream.write(bytes);
        }
    }
}
