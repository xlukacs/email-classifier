import { cva, type VariantProps } from "class-variance-authority"
import { cn } from "@/lib/utils"

const badgeVariants = cva(
  "inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium",
  {
    variants: {
      variant: {
        attention: "bg-wax/15 text-wax",
        review: "bg-brass/20 text-brass",
        skip: "bg-moss/15 text-moss",
        dry: "bg-lamp/20 text-lamp",
        mute: "bg-white/8 text-paper/70",
      },
    },
    defaultVariants: {
      variant: "mute",
    },
  },
)

function Badge({
  className,
  variant,
  ...props
}: React.ComponentProps<"span"> & VariantProps<typeof badgeVariants>) {
  return <span className={cn(badgeVariants({ variant }), className)} {...props} />
}

export { Badge, badgeVariants }
