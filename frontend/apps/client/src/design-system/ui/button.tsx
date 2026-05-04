import * as React from "react";
import { cn } from "@/lib/utils";

type ButtonVariant = "primary" | "secondary" | "ghost" | "outline";
type ButtonSize = "sm" | "md" | "lg" | "icon";

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

const variantClassName: Record<ButtonVariant, string> = {
  primary:
    "border border-transparent bg-[var(--primary)] text-white shadow-none hover:bg-[var(--primary-strong)]",
  secondary:
    "bg-[var(--primary-soft)] text-[var(--primary)] hover:bg-[#dfe7ff]",
  ghost: "bg-transparent text-[var(--foreground)] hover:bg-[#eef3ff]",
  outline:
    "border border-[rgba(31,41,55,0.16)] bg-white text-[var(--foreground)] shadow-none hover:border-[rgba(31,41,55,0.22)] hover:bg-[#fbfcff]",
};

const sizeClassName: Record<ButtonSize, string> = {
  sm: "h-9 rounded-xl px-3 text-sm",
  md: "h-11 rounded-2xl px-4 text-sm font-semibold",
  lg: "h-14 rounded-2xl px-6 text-base font-semibold",
  icon: "h-11 w-11 rounded-2xl p-0",
};

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant = "primary", size = "md", type = "button", ...props }, ref) => {
    return (
      <button
        ref={ref}
        type={type}
        className={cn(
          "inline-flex items-center justify-center gap-2 whitespace-nowrap transition duration-200 disabled:cursor-not-allowed disabled:opacity-50",
          variantClassName[variant],
          sizeClassName[size],
          className,
        )}
        {...props}
      />
    );
  },
);

Button.displayName = "Button";
