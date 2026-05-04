import { proxyCodeServingRequest } from "../../_genos";

export async function POST(request: Request) {
  const payload = await request.json();
  return proxyCodeServingRequest(payload);
}
