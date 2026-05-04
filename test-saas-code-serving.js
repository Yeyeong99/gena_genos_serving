let axios = null;
try {
  axios = require("axios");
} catch {
  axios = null;
}

const url = process.env.GENOS_SERVING_URL || "https://genos.genon.ai/api/gateway/code_serving/38/json";
const token = process.env.GENOS_TOKEN;

if (!token) {
  throw new Error("GENOS_TOKEN 환경변수를 설정해 주세요.");
}

const styleOptions = {
  purpose: process.env.TRANSLATION_PURPOSE || "presentation",
  formality: process.env.TRANSLATION_FORMALITY || "formal",
  terminology: process.env.TRANSLATION_TERMINOLOGY || "preserve",
};

function buildRequestData() {
  const fileUrl = process.env.TEST_FILE_URL;

  if (fileUrl) {
    const fileName = process.env.TEST_FILE_NAME || fileUrl.split("?")[0].split("/").pop() || "document.docx";
    return {
      sources: [
        {
          presigned_url: fileUrl,
          metadata: {
            file_name: fileName,
          },
        },
      ],
      format: process.env.TARGET_LANGUAGE || "Korean",
      style_options: styleOptions,
      is_return_file: process.env.IS_RETURN_FILE !== "0",
    };
  }

  return {
    input_text:
      process.env.TEST_INPUT_TEXT ||
      "Good morning. The meeting agenda includes revenue growth, hiring plans, and product launch risks.",
    format: process.env.TARGET_LANGUAGE || "Korean",
    style_options: styleOptions,
    is_return_file: false,
  };
}

async function main() {
  const requestData = buildRequestData();

  try {
    const headers = {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
    };
    const timeout = Number(process.env.GENOS_TIMEOUT_MS || 180000);
    const response = axios
      ? await axios.post(url, requestData, { headers, timeout })
      : await postWithFetch(url, requestData, { headers, timeout });

    if (typeof response.data === "string") {
      console.log(response.data);
    } else {
      console.log(JSON.stringify(response.data, null, 2));
    }
  } catch (error) {
    console.error(
      JSON.stringify(
        {
          error: error.message,
          status: error.response?.status,
          details: error.response ? error.response.data : "No response from server",
        },
        null,
        2,
      ),
    );
    process.exitCode = 1;
  }
}

async function postWithFetch(targetUrl, body, { headers, timeout }) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try {
    const response = await fetch(targetUrl, {
      method: "POST",
      headers,
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await response.text();
    let data = text;
    try {
      data = JSON.parse(text);
    } catch {
      // Keep non-JSON responses as-is for debugging.
    }
    if (!response.ok) {
      const error = new Error(`Request failed with status ${response.status}`);
      error.response = { status: response.status, data };
      throw error;
    }
    return { data };
  } finally {
    clearTimeout(timer);
  }
}

main();
