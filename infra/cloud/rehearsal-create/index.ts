import * as pulumi from "@pulumi/pulumi";
import { readInput, type RehearsalInput } from "./input";
import { declareResources } from "./resources";

let input: RehearsalInput;
try {
  if (pulumi.getProject() !== "terrace-cloud-rehearsal") throw new Error();
  input = readInput(process.env.TERRACE_REHEARSAL_INPUT, pulumi.getStack());
} catch {
  throw new Error("Private rehearsal input rejected");
}

export const owned = declareResources(input);
