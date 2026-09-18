package qimei;

import com.github.unidbg.AndroidEmulator;
import com.github.unidbg.Module;
import com.github.unidbg.arm.backend.DynarmicFactory;
import com.github.unidbg.linux.android.AndroidEmulatorBuilder;
import com.github.unidbg.linux.android.AndroidResolver;
import com.github.unidbg.linux.android.dvm.AbstractJni;
import com.github.unidbg.linux.android.dvm.BaseVM;
import com.github.unidbg.linux.android.dvm.DalvikModule;
import com.github.unidbg.linux.android.dvm.DvmClass;
import com.github.unidbg.linux.android.dvm.DvmObject;
import com.github.unidbg.linux.android.dvm.StringObject;
import com.github.unidbg.linux.android.dvm.VaList;
import com.github.unidbg.linux.android.dvm.VM;
import com.github.unidbg.linux.android.dvm.array.ArrayObject;
import com.github.unidbg.linux.android.dvm.array.ByteArray;
import com.github.unidbg.linux.android.dvm.jni.ProxyDvmObject;
import com.github.unidbg.memory.Memory;
import com.github.unidbg.pointer.UnidbgPointer;
import org.json.JSONObject;

import javax.crypto.Cipher;
import java.io.File;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.security.Key;
import java.security.KeyFactory;
import java.security.spec.KeySpec;
import java.security.spec.X509EncodedKeySpec;
import java.time.Duration;
import java.util.Arrays;
import java.util.Base64;
import java.util.Collections;
import java.util.Enumeration;

public final class QimeiProbe extends AbstractJni implements AutoCloseable {
    private static final String APP_KEY = "0AND0GZQH44TDFUA";
    private static final URI REGISTER_URI = URI.create("https://snowflake.qq.com/ola/v2");
    private final AndroidEmulator emulator;
    private final VM vm;
    private final DvmClass nativeApi;
    private final boolean live;
    private final boolean debug;
    private final JSONObject deviceProfile;
    private String capturedRequest;

    private QimeiProbe(
            File apk, File library, JSONObject deviceProfile, boolean live, boolean debug) {
        this.live = live;
        this.debug = debug;
        this.deviceProfile = deviceProfile;
        emulator = AndroidEmulatorBuilder.for64Bit()
                .addBackendFactory(new DynarmicFactory(true))
                .setProcessName("com.tencent.qt.qtl")
                .build();
        Memory memory = emulator.getMemory();
        memory.setLibraryResolver(new AndroidResolver(23));

        vm = emulator.createDalvikVM(apk);
        vm.setJni(this);
        vm.setVerbose(false);

        DalvikModule dalvikModule = vm.loadLibrary(library, false);
        Module module = dalvikModule.getModule();
        if (debug) {
            System.err.printf("loaded %s at 0x%x%n", module.name, module.base);
        }
        UnidbgPointer.pointer(emulator, module.base + 0x515ec)
                .setInt(0, 0x17ffff90);
        try {
            dalvikModule.callJNI_OnLoad(emulator);
        } catch (RuntimeException exception) {
            System.err.println(
                    "JNI_OnLoad self-check failed after native registration: "
                            + exception.getClass().getSimpleName());
        }
        nativeApi = vm.resolveClass("com/tencent/qimei/uin/U");
        DvmObject<?> application = vm.resolveClass("android/app/Application").newObject(null);
        DvmObject<?>[] callbacks = {
                callback("com/tencent/qimei/r/c", "sendSample", "(III)I", "k1"),
                callback("com/tencent/qimei/r/b", "sendError", "(III)I", "k2"),
                callback(
                        "com/tencent/qimei/p/a",
                        "getLauncherActivity",
                        "(Landroid/content/Context;)Landroid/app/Activity;",
                        "k3"),
                callback(
                        "com/tencent/qimei/y/d",
                        "p",
                        "(ILjava/lang/String;)Ljava/lang/String;",
                        "k4"),
                callback(
                        "com/tencent/qimei/y/b",
                        "s",
                        "(Ljava/lang/String;)Ljava/lang/String;",
                        "k5"),
                callback(
                        "com/tencent/qimei/y/a",
                        "g",
                        "(Ljava/lang/String;Ljava/lang/String;II)Ljava/lang/String;",
                        "k6"),
                callback("com/tencent/qimei/u/e", "s", "(Ljava/lang/String;)Z", "k7")
        };
        nativeApi.callStaticJniMethod(
                emulator,
                "n(Landroid/content/Context;Ljava/lang/String;[Ljava/lang/Object;Z)V",
                application,
                new StringObject(vm, APP_KEY),
                new ArrayObject(callbacks),
                false);
    }

    private ArrayObject callback(
            String className, String methodName, String methodSignature, String key) {
        return new ArrayObject(
                new StringObject(vm, className),
                new StringObject(vm, methodName),
                new StringObject(vm, methodSignature),
                new StringObject(vm, key));
    }

    private String generateRequest() {
        org.json.JSONArray configuredSdkInfo = deviceProfile.getJSONArray("sdkInfo");
        if (configuredSdkInfo.length() != 12) {
            throw new IllegalArgumentException(
                    "device profile sdkInfo must contain exactly 12 values");
        }
        String[] values = new String[configuredSdkInfo.length()];
        for (int index = 0; index < configuredSdkInfo.length(); index++) {
            values[index] = configuredSdkInfo.getString(index);
        }
        if (!APP_KEY.equals(values[0])) {
            throw new IllegalArgumentException("device profile contains an unexpected app key");
        }

        DvmObject<?>[] sdkInfo = Arrays.stream(values)
                .map(value -> new StringObject(vm, value))
                .toArray(DvmObject<?>[]::new);
        String deviceJson = deviceProfile.getJSONObject("deviceInfo").toString();
        int androidApi = deviceProfile.getInt("androidApi");

        DvmObject<?> result = nativeApi.callStaticJniMethodObject(
                emulator,
                "r(ZIILjava/lang/String;I[Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;",
                false,
                androidApi,
                1,
                new StringObject(vm, deviceJson),
                18,
                new ArrayObject(sdkInfo),
                new StringObject(vm, ""));
        if (result == null) {
            throw new IllegalStateException("U.r returned null");
        }
        if (capturedRequest != null) {
            if (debug) {
                System.err.println("captured-request=" + capturedRequest);
            }
        }
        return String.valueOf(result.getValue());
    }

    private String postRegistration(String requestBody) {
        try {
            HttpClient client = HttpClient.newBuilder()
                    .connectTimeout(Duration.ofSeconds(30))
                    .followRedirects(HttpClient.Redirect.NEVER)
                    .build();
            HttpRequest request = HttpRequest.newBuilder(REGISTER_URI)
                    .timeout(Duration.ofSeconds(10))
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(
                            requestBody, StandardCharsets.UTF_8))
                    .build();
            HttpResponse<String> response = client.send(
                    request, HttpResponse.BodyHandlers.ofString(StandardCharsets.UTF_8));
            if (response.statusCode() == 200) {
                return new JSONObject()
                        .put("is", 1)
                        .put("bd", response.body())
                        .put("rc", 0)
                        .toString();
            }
            return networkFailure(
                    "452",
                    response.statusCode(),
                    "response status code != 200");
        } catch (InterruptedException exception) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException("QIMEI registration interrupted", exception);
        } catch (Exception exception) {
            return networkFailure("499", -1, exception.toString());
        }
    }

    private static String networkFailure(String internalCode, int responseCode, String message) {
        return new JSONObject()
                .put("is", 0)
                .put("ic", internalCode)
                .put("rc", responseCode)
                .put("em", message)
                .toString();
    }

    private static String extractQimei36(String nativeResult) {
        JSONObject outer = new JSONObject(nativeResult);
        int error = outer.optInt("error", -1);
        if (error != 0) {
            throw new IllegalStateException(
                    "QIMEI registration failed: " + outer.optString("msg", nativeResult));
        }
        String body = outer.optString("body");
        if (body.isEmpty()) {
            throw new IllegalStateException("QIMEI response body is empty");
        }
        JSONObject qimeiBody = new JSONObject(body);
        String qimei36 = qimeiBody.optString("q36");
        if (qimei36.isEmpty()) {
            qimei36 = qimeiBody.optString("2");
        }
        if (qimei36.isEmpty()) {
            throw new IllegalStateException("QIMEI response does not contain q36: " + body);
        }
        return qimei36;
    }

    private static JSONObject decodeDeviceProfile(String encodedProfile) {
        try {
            byte[] decoded = Base64.getUrlDecoder().decode(encodedProfile);
            return new JSONObject(new String(decoded, StandardCharsets.UTF_8));
        } catch (IllegalArgumentException exception) {
            throw new IllegalArgumentException("invalid --profile-base64 value", exception);
        }
    }

    @Override
    public DvmObject<?> callStaticObjectMethodV(
            BaseVM vm, DvmClass dvmClass, String signature, VaList vaList) {
        if ("android/system/Os->stat(Ljava/lang/String;)Landroid/system/StructStat;"
                .equals(signature)
                || "android/system/Os->lstat(Ljava/lang/String;)Landroid/system/StructStat;"
                        .equals(signature)
                || "android/system/Os->statvfs(Ljava/lang/String;)Landroid/system/StructStatVfs;"
                        .equals(signature)) {
            StringObject path = vaList.getObjectArg(0);
            System.err.println("stat probe treated as absent: " + path.getValue());
            return null;
        }
        if ("java/lang/System->getProperty(Ljava/lang/String;Ljava/lang/String;)Ljava/lang/String;"
                .equals(signature)) {
            return vaList.getObjectArg(1);
        }
        if ("java/net/NetworkInterface->getNetworkInterfaces()Ljava/util/Enumeration;"
                .equals(signature)) {
            return vm.resolveClass("java/util/Enumeration")
                    .newObject(Collections.emptyEnumeration());
        }
        if ("com/tencent/qimei/y/d->p(ILjava/lang/String;)Ljava/lang/String;"
                .equals(signature)) {
            StringObject request = vaList.getObjectArg(1);
            capturedRequest = request.getValue();
            if (live) {
                return new StringObject(vm, postRegistration(capturedRequest));
            }
            return new StringObject(
                    vm,
                    networkFailure("offline", -1, "network disabled; pass --live"));
        }
        try {
            if ("java/security/KeyFactory->getInstance(Ljava/lang/String;)Ljava/security/KeyFactory;"
                    .equals(signature)) {
                StringObject algorithm = vaList.getObjectArg(0);
                return ProxyDvmObject.createObject(
                        vm, KeyFactory.getInstance(algorithm.getValue()));
            }
            if ("javax/crypto/Cipher->getInstance(Ljava/lang/String;)Ljavax/crypto/Cipher;"
                    .equals(signature)) {
                StringObject transformation = vaList.getObjectArg(0);
                return ProxyDvmObject.createObject(
                        vm, Cipher.getInstance(transformation.getValue()));
            }
        } catch (Exception exception) {
            throw new IllegalStateException("JCA static call failed: " + signature, exception);
        }
        return super.callStaticObjectMethodV(vm, dvmClass, signature, vaList);
    }

    @Override
    public int callStaticIntMethodV(
            BaseVM vm, DvmClass dvmClass, String signature, VaList vaList) {
        if ("android/provider/Settings$Secure->getInt(Landroid/content/ContentResolver;Ljava/lang/String;I)I"
                .equals(signature)) {
            return vaList.getIntArg(2);
        }
        if ("com/tencent/qimei/r/c->sendSample(III)I".equals(signature)
                || "com/tencent/qimei/r/b->sendError(III)I".equals(signature)) {
            return 0;
        }
        return super.callStaticIntMethodV(vm, dvmClass, signature, vaList);
    }

    @Override
    public boolean callStaticBooleanMethodV(
            BaseVM vm, DvmClass dvmClass, String signature, VaList vaList) {
        if ("com/tencent/qimei/u/e->s(Ljava/lang/String;)Z".equals(signature)) {
            return false;
        }
        return super.callStaticBooleanMethodV(vm, dvmClass, signature, vaList);
    }

    @Override
    public boolean callBooleanMethodV(
            BaseVM vm, DvmObject<?> dvmObject, String signature, VaList vaList) {
        if ("android/view/accessibility/AccessibilityManager->isEnabled()Z"
                .equals(signature)) {
            return false;
        }
        if ("java/util/Enumeration->hasMoreElements()Z".equals(signature)) {
            @SuppressWarnings("unchecked")
            Enumeration<Object> enumeration = (Enumeration<Object>) dvmObject.getValue();
            return enumeration.hasMoreElements();
        }
        return super.callBooleanMethodV(vm, dvmObject, signature, vaList);
    }

    @Override
    public DvmObject<?> callObjectMethodV(
            BaseVM vm, DvmObject<?> dvmObject, String signature, VaList vaList) {
        if ("android/app/Application->getApplicationInfo()Landroid/content/pm/ApplicationInfo;"
                .equals(signature)) {
            return vm.resolveClass("android/content/pm/ApplicationInfo").newObject(null);
        }
        if ("android/app/Application->getPackageName()Ljava/lang/String;"
                .equals(signature)) {
            return new StringObject(vm, vm.getPackageName());
        }
        if ("android/app/Application->getFilesDir()Ljava/io/File;"
                .equals(signature)) {
            return ProxyDvmObject.createObject(
                    vm, new File("/data/user/0/com.tencent.qt.qtl/files"));
        }
        if ("java/io/File->toString()Ljava/lang/String;".equals(signature)
                || "java/io/File->getAbsolutePath()Ljava/lang/String;".equals(signature)) {
            return new StringObject(vm, ((File) dvmObject.getValue()).getAbsolutePath());
        }
        if ("android/content/pm/Signature->toByteArray()[B".equals(signature)) {
            return new ByteArray(vm, (byte[]) dvmObject.getValue());
        }
        try {
            if ("java/security/KeyFactory->generatePublic(Ljava/security/spec/KeySpec;)Ljava/security/PublicKey;"
                    .equals(signature)) {
                DvmObject<?> keySpec = vaList.getObjectArg(0);
                return ProxyDvmObject.createObject(
                        vm, ((KeyFactory) dvmObject.getValue())
                                .generatePublic((KeySpec) keySpec.getValue()));
            }
            if ("javax/crypto/Cipher->doFinal([B)[B".equals(signature)) {
                ByteArray input = vaList.getObjectArg(0);
                Cipher cipher = (Cipher) dvmObject.getValue();
                if (debug) {
                    System.err.printf(
                            "cipher-input algorithm=%s value=%s%n",
                            cipher.getAlgorithm(),
                            Base64.getEncoder().encodeToString(input.getValue()));
                }
                return new ByteArray(
                        vm, cipher.doFinal(input.getValue()));
            }
        } catch (Exception exception) {
            throw new IllegalStateException("JCA object call failed: " + signature, exception);
        }
        if ("java/lang/Class->getDeclaredField(Ljava/lang/String;)Ljava/lang/reflect/Field;"
                .equals(signature)) {
            StringObject fieldName = vaList.getObjectArg(0);
            System.err.println("unsupported reflected field: " + fieldName.getValue());
            return null;
        }
        return super.callObjectMethodV(vm, dvmObject, signature, vaList);
    }

    @Override
    public DvmObject<?> newObjectV(
            BaseVM vm, DvmClass dvmClass, String signature, VaList vaList) {
        if ("java/security/spec/X509EncodedKeySpec-><init>([B)V".equals(signature)) {
            ByteArray encoded = vaList.getObjectArg(0);
            if (debug) {
                System.err.println(
                        "rsa-public-key-der="
                                + Base64.getEncoder().encodeToString(encoded.getValue()));
            }
            return ProxyDvmObject.createObject(
                    vm, new X509EncodedKeySpec(encoded.getValue()));
        }
        return super.newObjectV(vm, dvmClass, signature, vaList);
    }

    @Override
    public void callVoidMethodV(
            BaseVM vm, DvmObject<?> dvmObject, String signature, VaList vaList) {
        if ("javax/crypto/Cipher->init(ILjava/security/Key;)V".equals(signature)) {
            int mode = vaList.getIntArg(0);
            DvmObject<?> key = vaList.getObjectArg(1);
            try {
                ((Cipher) dvmObject.getValue()).init(mode, (Key) key.getValue());
                return;
            } catch (Exception exception) {
                throw new IllegalStateException("Cipher initialization failed", exception);
            }
        }
        super.callVoidMethodV(vm, dvmObject, signature, vaList);
    }

    @Override
    public DvmObject<?> getObjectField(
            BaseVM vm, DvmObject<?> dvmObject, String signature) {
        if ("android/content/pm/ApplicationInfo->sourceDir:Ljava/lang/String;"
                .equals(signature)) {
            return new StringObject(vm, "/data/app/com.tencent.qt.qtl/base.apk");
        }
        if ("android/content/pm/PackageInfo->signatures:[Landroid/content/pm/Signature;"
                .equals(signature)) {
            DvmClass signatureClass = vm.resolveClass("android/content/pm/Signature");
            DvmObject<?>[] signatures = Arrays.stream(vm.getSignatures())
                    .map(certificate -> signatureClass.newObject(certificate.getData()))
                    .toArray(DvmObject<?>[]::new);
            return new ArrayObject(signatures);
        }
        return super.getObjectField(vm, dvmObject, signature);
    }

    @Override
    public void close() throws Exception {
        emulator.close();
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 4) {
            throw new IllegalArgumentException(
                    "usage: QimeiProbe <target.apk> <libqimei.so> "
                            + "--profile-base64 <value> [--live] [--debug]");
        }
        boolean live = Arrays.asList(args).contains("--live");
        boolean debug = Arrays.asList(args).contains("--debug");
        int profileOption = Arrays.asList(args).indexOf("--profile-base64");
        if (profileOption < 0 || profileOption + 1 >= args.length) {
            throw new IllegalArgumentException("--profile-base64 requires a value");
        }
        JSONObject deviceProfile = decodeDeviceProfile(args[profileOption + 1]);
        try (QimeiProbe probe =
                     new QimeiProbe(
                             new File(args[0]),
                             new File(args[1]),
                             deviceProfile,
                             live,
                             debug)) {
            String result = probe.generateRequest();
            if (live) {
                System.out.println(extractQimei36(result));
            } else {
                System.out.println(probe.capturedRequest);
            }
        }
    }

    /**
     * In-process live registration: emulate the native library and return the
     * 36-character QIMEI36. Used by the HTTP service so it never shells out.
     */
    public static String register(File apk, File library, JSONObject deviceProfile, boolean debug)
            throws Exception {
        try (QimeiProbe probe = new QimeiProbe(apk, library, deviceProfile, true, debug)) {
            return extractQimei36(probe.generateRequest());
        }
    }
}
