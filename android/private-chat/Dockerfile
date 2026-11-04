FROM --platform=linux/amd64 eclipse-temurin:17-jdk-jammy
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip python3 python3-pip git && rm -rf /var/lib/apt/lists/*
ENV ANDROID_HOME=/opt/android-sdk
ENV PATH="/opt/gradle/bin:/opt/android-sdk/cmdline-tools/latest/bin:${PATH}"
RUN curl -fsSL https://services.gradle.org/distributions/gradle-8.9-bin.zip -o /tmp/g.zip && unzip -q /tmp/g.zip -d /opt && mv /opt/gradle-8.9 /opt/gradle && rm /tmp/g.zip
RUN mkdir -p /opt/android-sdk/cmdline-tools && curl -fsSL https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip -o /tmp/sdk.zip && unzip -q /tmp/sdk.zip -d /opt/android-sdk/cmdline-tools && mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest && rm /tmp/sdk.zip
RUN yes | sdkmanager --licenses >/dev/null && sdkmanager "platforms;android-35" "build-tools;35.0.0"
WORKDIR /source
