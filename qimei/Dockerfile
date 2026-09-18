# Build the shaded jar with Maven + JDK 17.
FROM maven:3.9-eclipse-temurin-17 AS build
WORKDIR /build
COPY pom.xml ./
# Warm the dependency cache before copying sources.
RUN mvn -q -B dependency:go-offline
COPY src ./src
RUN mvn -q -B package

# Runtime: JRE + native lib. QIMEI36 is emulated via Unidbg (ARM64 in software),
# so this runs on any host arch.
FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /build/target/qimei-service.jar ./qimei-service.jar
COPY lib/ ./lib/

ENV QIMEI_DATA_DIR=/data \
    QIMEI_SO_PATH=/app/lib/libqimei.so \
    QIMEI_PORT=8080
EXPOSE 8080
VOLUME ["/data"]

CMD ["java", "-jar", "qimei-service.jar"]
